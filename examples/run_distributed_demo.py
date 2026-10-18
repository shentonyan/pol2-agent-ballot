"""Exercise offline custody and a witness in separate CLI processes.

Run: python examples/run_distributed_demo.py --directory PATH
PATH must be new. It contains DEMO private identities: keep it outside Git.
Different directories on one host demonstrate the protocol, not OS isolation.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pol2_agent_ballot.ballot import Ballot, Envelope
from pol2_agent_ballot.election import Election, ElectionConfig
from pol2_agent_ballot.evidence import local_path, read_json, write_new
from pol2_agent_ballot.identity import AgentIdentity, build_registry
from pol2_agent_ballot.tally import tally


def run(module, *args):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONUTF8="1")
    result = subprocess.run([sys.executable, "-m", "pol2_agent_ballot." + module, *map(str, args)],
                            env=env, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout)
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    args = parser.parse_args()
    root = local_path(args.directory)
    root.mkdir(parents=True, exist_ok=False)
    organiser = AgentIdentity.generate("demo-organiser")
    voters = [AgentIdentity.generate(f"voter-{i}") for i in range(3)]
    witness_state, store = root / "witness" / "secret.json", root / "witness" / "evidence"
    witness = json.loads(run("evidence", "init", "--state", witness_state, "--store", store,
                             "--organiser", organiser.agent_id))["witness"]
    processor_state, processor_public = root / "key-app" / "secret.json", root / "processor-public.json"
    run("custody", "init", "--state", processor_state, "--role", "processor",
        "--organiser", organiser.agent_id, "--witness", witness, "--public-output", processor_public)
    processor = read_json(processor_public)["encryption_key"]
    custodians = []
    for i in range(3):
        secret, public = root / f"custodian-{i}" / "secret.json", root / f"custodian-{i}-public.json"
        run("custody", "init", "--state", secret, "--role", "custodian", "--organiser", organiser.agent_id,
            "--witness", witness, "--processor", processor, "--public-output", public)
        custodians.append((secret, public))
    cfg = ElectionConfig("distributed-demo", "Advance?", ["advance", "hold"])
    config, dealer, manifest = root / "config.json", root / "dealer" / "organiser.json", root / "manifest.json"
    write_new(config, json.loads(cfg.canonical()))
    write_new(dealer, {"signing_key": organiser.signing_key.encode().hex()}, secret=True)
    run("custody", "ceremony", "--config", config, "--organiser-secret", dealer,
        "--custodians", *[p for _, p in custodians], "--processor", processor, "--output", manifest)
    election = Election(cfg, build_registry(organiser, voters), organiser)
    election.install_custody_manifest(read_json(manifest))
    election.open()
    for i, voter in enumerate(voters):
        ballot = Ballot(cfg.election_id, voter.agent_id, "advance" if i < 2 else "hold", "demo evidence")
        receipt = election.accept(Envelope.cast(voter, ballot, election.public_key))
        path = root / f"receipt-{i}.json"
        write_new(path, receipt)
        run("evidence", "receipt", "--state", witness_state, "--store", store, "--input", path)
    election.close()
    closed, cp, decrypted = root / "closed.json", root / "close-checkpoint.json", root / "decrypted.json"
    write_new(closed, {"log": election.export_log(), "envelopes": election.export_envelopes()})
    run("evidence", "checkpoint", "--state", witness_state, "--store", store,
        "--input", closed, "--output", cp)
    releases = []
    for i, (secret, _) in enumerate(custodians[:2]):
        path = root / f"release-{i}.json"
        run("custody", "release", "--state", secret, "--manifest", manifest, "--export", closed,
            "--checkpoint", cp, "--output", path)
        releases.append(path)
    run("custody", "decrypt", "--state", processor_state, "--manifest", manifest, "--export", closed,
        "--checkpoint", cp, "--releases", *releases, "--output", decrypted)
    election.import_decryption(read_json(decrypted))
    result = tally(election)
    final = root / "final.json"
    write_new(final, {"log": election.export_log(), "envelopes": election.export_envelopes()})
    run("evidence", "checkpoint", "--state", witness_state, "--store", store, "--input", final)
    report = json.loads(run("evidence", "audit", "--organiser", organiser.agent_id, "--witness", witness, "--store", store, "--input", final))
    print(json.dumps({"audit_ok": report["ok"], "winner": result.winner,
                      "collector_retained_shares": len(election.shares), "released_custodians": len(releases),
                      "directory": str(root)}))


if __name__ == "__main__":
    main()
