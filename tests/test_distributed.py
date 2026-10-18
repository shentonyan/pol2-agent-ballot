"""Integration tests for independent custody and external evidence."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from nacl.signing import SigningKey

from pol2_agent_ballot.ballot import Ballot, Envelope, canonical
from pol2_agent_ballot.custody import ceremony, decrypt_export, initialise, release
from pol2_agent_ballot.election import Election, ElectionConfig
from pol2_agent_ballot.evidence import WitnessStore, read_json, write_new
from pol2_agent_ballot.identity import AgentIdentity, build_registry
from pol2_agent_ballot.tally import tally
from pol2_agent_ballot.verify import Auditor


def cli(module, *args, ok=True):
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"), PYTHONUTF8="1")
    result = subprocess.run([sys.executable, "-m", "pol2_agent_ballot." + module, *map(str, args)],
                            capture_output=True, text=True, env=env, timeout=20)
    if ok:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0
    return result


@pytest.fixture
def setup(tmp_path):
    org = AgentIdentity.generate("organiser")
    voter = AgentIdentity.generate("voter")
    witness_key = SigningKey.generate()
    witness = WitnessStore(tmp_path / "witness", org.agent_id, witness_key)
    processor, processor_public = initialise("processor", org.agent_id, witness.public_key)
    custodians = [initialise("custodian", org.agent_id, witness.public_key,
                             processor_public["encryption_key"]) for _ in range(3)]
    cfg = ElectionConfig("distributed", "advance?", ["advance", "hold"])
    manifest = ceremony(json.loads(cfg.canonical()), [p for _, p in custodians],
                        processor_public["encryption_key"], org.signing_key)
    el = Election(cfg, build_registry(org, [voter]), org)
    el.install_custody_manifest(manifest)
    el.open()
    env = Envelope.cast(voter, Ballot(cfg.election_id, voter.agent_id, "advance", "checked"), el.public_key)
    witness.save_receipt(el.accept(env))
    return el, org, voter, witness, processor, custodians, manifest


def closed(setup):
    el, org, voter, witness, processor, custodians, manifest = setup
    el.close()
    export = {"log": el.export_log(), "envelopes": el.export_envelopes()}
    cp = witness.checkpoint(**export)
    return export, cp


def test_separate_processes_complete_election_and_reopen_evidence_store(setup, tmp_path):
    el, org, voter, witness, processor, custodians, manifest = setup
    export, cp = closed(setup)
    assert el.shares == [] and el._sk is None and el.recovered_sk is None
    assert processor["encryption_secret"] not in json.dumps(manifest)
    paths = {}
    for name, value in [("manifest", manifest), ("closed", export), ("checkpoint", cp), ("processor", processor)]:
        paths[name] = tmp_path / (name + ".json")
        write_new(paths[name], value, secret=name == "processor")
    releases = []
    for index in range(2):
        state = tmp_path / f"custodian-{index}" / "state.json"
        write_new(state, custodians[index][0], secret=True)
        out = tmp_path / f"release-{index}.json"
        cli("custody", "release", "--state", state, "--manifest", paths["manifest"],
            "--export", paths["closed"], "--checkpoint", paths["checkpoint"], "--output", out)
        releases.append(out)
        assert "share" not in read_json(out)["payload"]  # only sealed shares leave custodians
    output = tmp_path / "decrypted.json"
    cli("custody", "decrypt", "--state", paths["processor"], "--manifest", paths["manifest"],
        "--export", paths["closed"], "--checkpoint", paths["checkpoint"], "--releases", *releases,
        "--output", output)
    el.import_decryption(read_json(output))
    assert tally(el).winner == "advance"
    again = WitnessStore(witness.root, org.agent_id, witness.key)
    assert len(again.receipts(el.config.election_id)) == 1
    assert again.checkpoints(el.config.election_id) == [cp]
    final_cp = again.checkpoint(el.export_log(), el.export_envelopes())
    report = Auditor(el.export_log(), el.export_envelopes(), trusted_organiser=org.agent_id,
                     receipts=again.receipts(el.config.election_id), checkpoints=[cp, final_cp],
                     trusted_witnesses=[again.public_key], require_complete=True).run()
    assert report.ok, report.failures
    # Final checkpoint makes earlier closed records insufficient for a final audit.
    assert not Auditor(export["log"], export["envelopes"], checkpoints=[final_cp],
                       trusted_witnesses=[again.public_key]).run().ok


def test_release_rejects_open_election_even_with_witness_checkpoint(setup):
    el, _, _, witness, _, custodians, manifest = setup
    export = {"log": el.export_log(), "envelopes": el.export_envelopes()}
    cp = witness.checkpoint(**export)
    with pytest.raises(ValueError, match="closing checkpoint"):
        release(custodians[0][0], manifest, export, cp)


@pytest.mark.parametrize("change", ["bad_witness", "bad_checkpoint", "bad_manifest", "wrong_processor", "unknown_custodian"])
def test_release_rejects_wrong_trust_or_context(setup, change):
    el, _, _, witness, processor, custodians, manifest = setup
    export, cp = closed(setup)
    state = copy.deepcopy(custodians[0][0])
    manifest, cp = copy.deepcopy(manifest), copy.deepcopy(cp)
    if change == "bad_witness":
        state["witness"] = "00" * 32
    elif change == "bad_checkpoint":
        cp["payload"]["log_hash"] = "00" * 32
    elif change == "bad_manifest":
        manifest["payload"]["election_id"] = "other-election"
    elif change == "wrong_processor":
        state["processor"] = "00" * 32
    else:
        state, _ = initialise("custodian", state["organiser"], state["witness"], state["processor"])
    with pytest.raises(ValueError):
        release(state, manifest, export, cp)


@pytest.mark.parametrize("change", ["insufficient", "duplicate", "tampered", "wrong_processor"])
def test_key_application_rejects_invalid_releases(setup, change):
    _, _, _, _, processor, custodians, manifest = setup
    export, cp = closed(setup)
    releases = [release(state, manifest, export, cp) for state, _ in custodians[:2]]
    state = copy.deepcopy(processor)
    if change == "insufficient":
        releases.pop()
    elif change == "duplicate":
        releases[1] = releases[0]
    elif change == "tampered":
        releases[0]["payload"]["close_hash"] = "00" * 32
    else:
        state, _ = initialise("processor", state["organiser"], state["witness"])
    with pytest.raises(ValueError):
        decrypt_export(state, manifest, export, cp, releases)


def test_retained_receipt_detects_joint_ballot_and_log_omission(setup):
    el, org, _, witness, _, _, _ = setup
    # This looks like a legitimate unfinished election without the ballot.
    log = el.export_log()[:-1]
    assert Auditor(log, []).run().ok
    report = Auditor(log, [], trusted_organiser=org.agent_id,
                     receipts=witness.receipts(el.config.election_id)).run()
    assert not report.ok and any("external evidence" in f for f in report.failures)


def test_checkpoint_detects_complete_tail_truncation_and_wrong_witness(setup):
    el, org, _, witness, _, _, _ = setup
    export, cp = closed(setup)
    truncated = export["log"][:-1]
    assert Auditor(truncated, export["envelopes"]).run().ok
    assert not Auditor(truncated, export["envelopes"], checkpoints=[cp],
                       trusted_witnesses=[witness.public_key]).run().ok
    assert not Auditor(export["log"], export["envelopes"], checkpoints=[cp],
                       trusted_witnesses=["00" * 32]).run().ok
    assert not Auditor(export["log"], export["envelopes"], trusted_organiser="00" * 32).run().ok


def test_witness_rejects_rollback_and_tampered_receipts(setup):
    el, _, _, witness, _, _, _ = setup
    export, cp = closed(setup)
    with pytest.raises(ValueError):
        witness.checkpoint(export["log"][:-1], export["envelopes"])
    receipt = copy.deepcopy(el.export_envelopes()[0]["receipt"])
    witness.save_receipt(receipt)  # retries are idempotent
    receipt["collector_signature"] = "00" * 64
    with pytest.raises(ValueError):
        witness.save_receipt(receipt)


def test_key_application_cannot_change_closed_history(setup):
    el, _, _, _, processor, custodians, manifest = setup
    export, cp = closed(setup)
    releases = [release(state, manifest, export, cp) for state, _ in custodians[:2]]
    decrypted = decrypt_export(processor, manifest, export, cp, releases)
    decrypted["log"][0]["payload"]["config"]["question"] = "different question"
    with pytest.raises(ValueError, match="closed record"):
        el.import_decryption(decrypted)


def test_private_state_is_not_overwritten(tmp_path):
    path = tmp_path / "identity.json"
    write_new(path, {"secret": "first"}, secret=True)
    with pytest.raises(ValueError, match="overwrite"):
        write_new(path, {"secret": "second"}, secret=True)
    assert read_json(path) == {"secret": "first"}


def test_witness_cli_and_custody_cli_init_and_ceremony(tmp_path):
    org = AgentIdentity.generate("org")
    witness_state = tmp_path / "witness" / "identity.json"
    store = tmp_path / "independent-receipts"
    result = cli("evidence", "init", "--state", witness_state, "--store", store, "--organiser", org.agent_id)
    witness = json.loads(result.stdout)["witness"]
    ps, pp = tmp_path / "processor" / "secret.json", tmp_path / "processor-public.json"
    cli("custody", "init", "--state", ps, "--role", "processor", "--organiser", org.agent_id,
        "--witness", witness, "--public-output", pp)
    processor = read_json(pp)["encryption_key"]
    public_paths = []
    for index in range(3):
        public = tmp_path / f"custodian-{index}-public.json"
        cli("custody", "init", "--state", tmp_path / f"custodian-{index}" / "secret.json",
            "--role", "custodian", "--organiser", org.agent_id, "--witness", witness,
            "--processor", processor, "--public-output", public)
        public_paths.append(public)
    cfg = ElectionConfig("cli", "?", ["advance", "hold"])
    config, secret, manifest_path = tmp_path / "config.json", tmp_path / "dealer.json", tmp_path / "manifest.json"
    write_new(config, json.loads(cfg.canonical()))
    write_new(secret, {"signing_key": org.signing_key.encode().hex()}, secret=True)
    cli("custody", "ceremony", "--config", config, "--custodians", *public_paths,
        "--processor", processor, "--organiser-secret", secret, "--output", manifest_path)
    voter = AgentIdentity.generate("voter")
    election = Election(cfg, build_registry(org, [voter]), org)
    election.install_custody_manifest(read_json(manifest_path))
    election.open()
    receipt = election.accept(Envelope.cast(voter, Ballot("cli", voter.agent_id, "hold"), election.public_key))
    rp, ep, cp = tmp_path / "receipt.json", tmp_path / "closed.json", tmp_path / "checkpoint.json"
    write_new(rp, receipt)
    cli("evidence", "receipt", "--state", witness_state, "--store", store, "--input", rp)
    election.close()
    write_new(ep, {"log": election.export_log(), "envelopes": election.export_envelopes()})
    cli("evidence", "checkpoint", "--state", witness_state, "--store", store, "--input", ep, "--output", cp)
    cli("evidence", "audit", "--organiser", org.agent_id, "--witness", witness, "--store", store, "--input", ep, ok=False)
    # Caller must supply close evidence; CLI never releases on missing input.
    cli("custody", "release", "--state", tmp_path / "custodian-0" / "secret.json", ok=False)


@pytest.mark.parametrize("change", ["missing_release", "duplicate_release", "bad_close", "wrong_indices"])
def test_auditor_checks_public_custodian_authorisations(setup, change):
    from pol2_agent_ballot.ballot import sha256
    el, org, _, _, processor, custodians, manifest = setup
    export, cp = closed(setup)
    records = [release(state, manifest, export, cp) for state, _ in custodians[:2]]
    out = decrypt_export(processor, manifest, export, cp, records)
    payload = out["log"][-1]["payload"]
    if change == "missing_release":
        payload["custodian_releases"].pop()
    elif change == "duplicate_release":
        payload["custodian_releases"][1] = payload["custodian_releases"][0]
    elif change == "bad_close":
        payload["closing_checkpoint"]["signature"] = "00" * 64
    else:
        payload["shares_used"] = [1, 1]
    entry = out["log"][-1]
    entry["hash"] = sha256(canonical({k: entry[k] for k in ("seq", "kind", "payload", "prev_hash", "ts")}))
    assert not Auditor(out["log"], out["envelopes"], trusted_organiser=org.agent_id).run().ok


@pytest.mark.parametrize("relative", [False, True])
def test_full_distributed_demo(tmp_path, relative):
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"), PYTHONUTF8="1")
    directory = os.path.relpath(tmp_path / "demo") if relative else str(tmp_path / "demo")
    result = subprocess.run([sys.executable, "examples/run_distributed_demo.py", "--directory", directory],
                            env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["audit_ok"] and report["winner"] == "advance"
    assert report["collector_retained_shares"] == 0 and report["released_custodians"] == 2


@pytest.mark.parametrize("field,value", [("election_id", "elsewhere"), ("close_hash", "00" * 32),
                                         ("manifest_hash", "00" * 32), ("processor", "00" * 32)])
def test_correctly_signed_release_cannot_be_replayed_in_wrong_context(setup, field, value):
    from pol2_agent_ballot.evidence import signed
    _, _, _, _, processor, custodians, manifest = setup
    export, cp = closed(setup)
    records = [release(state, manifest, export, cp) for state, _ in custodians[:2]]
    payload = records[0]["payload"]
    payload[field] = value
    records[0] = signed(payload, SigningKey(bytes.fromhex(custodians[0][0]["signing_secret"])))
    with pytest.raises(ValueError, match="different election or close"):
        decrypt_export(processor, manifest, export, cp, records)


def test_witness_does_not_overwrite_or_ignore_corrupted_retained_evidence(setup):
    el, _, _, witness, _, _, _ = setup
    path = next((witness._dir(el.config.election_id) / "receipts").glob("*.json"))
    path.write_text("not-json", encoding="utf-8")
    el.close()
    with pytest.raises(ValueError):
        witness.checkpoint(el.export_log(), el.export_envelopes())


def test_checkpoint_concurrent_writer_fails_closed(setup):
    el, _, _, witness, _, _, _ = setup
    export, _ = closed(setup)
    lock = witness._dir(el.config.election_id) / "checkpoints" / "writer.lock"
    lock.write_text("active", encoding="utf-8")
    with pytest.raises(ValueError, match="writer already active"):
        witness.checkpoint(**export)
    assert lock.exists()
