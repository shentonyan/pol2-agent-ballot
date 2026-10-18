"""Offline dealer, separate custodian release processes and key application.

Each CLI invocation reads only that role's private state. The dealer is trusted
and temporary (not distributed key generation). Release requires an independently
witnessed close, and packets are encrypted to the pinned key application.
"""
from __future__ import annotations

import argparse
import json
import time

from nacl.encoding import HexEncoder
from nacl.exceptions import CryptoError
from nacl.public import PrivateKey, PublicKey, SealedBox
from nacl.signing import SigningKey

from .ballot import Ballot, Envelope, canonical, sha256
from .crypto import KeyShare, generate_election_keypair, recover_private_key, split_private_key, unseal
from .election import ElectionConfig, LogEntry
from .evidence import read_json, signed, verify_signed, write_new
from .verify import Auditor


def initialise(role, organiser, witness, processor=None):
    from nacl.signing import VerifyKey

    VerifyKey(organiser, encoder=HexEncoder)
    VerifyKey(witness, encoder=HexEncoder)
    if role not in ("custodian", "processor"):
        raise ValueError("unknown custody role")
    if role == "custodian":
        PublicKey(processor, encoder=HexEncoder)
    encryption = PrivateKey.generate()
    signing = SigningKey.generate()
    state = {"role": role, "organiser": organiser, "witness": witness,
             "processor": processor, "encryption_secret": bytes(encryption).hex(),
             "signing_secret": signing.encode().hex()}
    public = {"role": role, "organiser": organiser, "witness": witness, "processor": processor, "encryption_key": encryption.public_key.encode(HexEncoder).decode(),
              "signing_key": signing.verify_key.encode(HexEncoder).decode()}
    return state, public


def ceremony(config, custodians, processor, organiser_key):
    """Run in an offline dealer, never in the collector. Return encrypted shares."""
    cfg = ElectionConfig(**config)
    if len(custodians) != cfg.threshold_n:
        raise ValueError("custodian count differs from signed configuration")
    if (len({c["signing_key"] for c in custodians}) != len(custodians)
            or len({c["encryption_key"] for c in custodians}) != len(custodians)):
        raise ValueError("custodian identities and encryption keys must be distinct")
    PublicKey(processor, encoder=HexEncoder)
    witness = custodians[0]["witness"]
    organiser = organiser_key.verify_key.encode(HexEncoder).decode()
    if any(c["organiser"] != organiser or c["witness"] != witness
           or c["processor"] != processor for c in custodians):
        raise ValueError("custodians disagree on pinned organiser, witness or processor")
    sk, pk = generate_election_keypair()
    shares = split_private_key(sk, cfg.threshold_k, cfg.threshold_n)
    public_key = pk.encode(HexEncoder).decode()
    config_hash = sha256(cfg.canonical())
    packets = []
    for share, custodian in zip(shares, custodians):
        if custodian["role"] != "custodian":
            raise ValueError("expected custodian public identity")
        packet = {"election_id": cfg.election_id, "config_hash": config_hash,
                  "public_key": public_key, "share": share.to_dict()}
        encrypted = SealedBox(PublicKey(custodian["encryption_key"], encoder=HexEncoder)).encrypt(canonical(packet))
        packets.append({"index": share.index, "custodian": custodian["signing_key"],
                        "encryption_key": custodian["encryption_key"], "sealed_share": encrypted.hex()})
    return signed({"type": "pol2-custody-v1", "election_id": cfg.election_id,
                   "config_hash": config_hash, "public_key": public_key,
                   "k": cfg.threshold_k, "n": cfg.threshold_n,
                   "processor": processor, "witness": witness, "custodians": packets}, organiser_key)


def validate_manifest(manifest, config, organiser):
    body = verify_signed(manifest, organiser)
    cfg = ElectionConfig(**config)
    if (body["type"] != "pol2-custody-v1" or body["election_id"] != cfg.election_id
            or body["config_hash"] != sha256(cfg.canonical())
            or body["k"] != cfg.threshold_k or body["n"] != cfg.threshold_n
            or len(body["custodians"]) != cfg.threshold_n):
        raise ValueError("custody manifest differs from signed election configuration")
    members = body["custodians"]
    if ({c["index"] for c in members} != set(range(1, cfg.threshold_n + 1))
            or len({c["custodian"] for c in members}) != cfg.threshold_n
            or len({c["encryption_key"] for c in members}) != cfg.threshold_n):
        raise ValueError("invalid or duplicate custodian membership")
    PublicKey(body["public_key"], encoder=HexEncoder)
    PublicKey(body["processor"], encoder=HexEncoder)
    return body


def verify_close(export, manifest, state, checkpoint):
    log, envelopes = export["log"], export["envelopes"]
    body = validate_manifest(manifest, log[0]["payload"]["config"], state["organiser"])
    if body["witness"] != state["witness"]:
        raise ValueError("witness differs from pinned custody identity")
    report = Auditor(log, envelopes, trusted_organiser=state["organiser"],
                     checkpoints=[checkpoint], trusted_witnesses=[state["witness"]]).run()
    if not report.ok:
        raise ValueError("closed export audit failed: " + "; ".join(report.failures))
    cp = verify_signed(checkpoint, state["witness"])
    if log[-1]["kind"] != "close" or cp["phase"] != "close" or cp["seq"] != len(log) - 1:
        raise ValueError("release requires an exact witness-signed closing checkpoint")
    ceremony_entry = next(e["payload"] for e in log if e["kind"] == "key_ceremony")
    if (ceremony_entry.get("custody_manifest") != manifest
            or ceremony_entry["public_key"] != body["public_key"]
            or ceremony_entry["k"] != body["k"] or ceremony_entry["n"] != body["n"]):
        raise ValueError("election key ceremony differs from custody manifest")
    return body, cp


def release(state, manifest, export, checkpoint):
    if state["role"] != "custodian":
        raise ValueError("only a custodian can release a share")
    body, cp = verify_close(export, manifest, state, checkpoint)
    if body["processor"] != state["processor"]:
        raise ValueError("key application differs from pinned processor")
    signing = SigningKey(bytes.fromhex(state["signing_secret"]))
    custodian = signing.verify_key.encode(HexEncoder).decode()
    member = next((c for c in body["custodians"] if c["custodian"] == custodian), None)
    if member is None:
        raise ValueError("custodian not in ceremony manifest")
    private = PrivateKey(bytes.fromhex(state["encryption_secret"]))
    if private.public_key.encode(HexEncoder).decode() != member["encryption_key"]:
        raise ValueError("custodian encryption key mismatch")
    packet = json.loads(SealedBox(private).decrypt(bytes.fromhex(member["sealed_share"])))
    if (packet["election_id"] != body["election_id"]
            or packet["config_hash"] != body["config_hash"]
            or packet["public_key"] != body["public_key"]
            or packet["share"]["index"] != member["index"]):
        raise ValueError("decrypted share is not bound to this election")
    payload = {"type": "pol2-release-v1", "election_id": body["election_id"],
               "manifest_hash": sha256(canonical(manifest)), "close_hash": cp["log_hash"],
               "index": member["index"], "processor": body["processor"],
               "sealed_share": SealedBox(PublicKey(body["processor"], encoder=HexEncoder)).encrypt(canonical(packet)).hex()}
    return signed(payload, signing)


def verify_release_attestations(manifest, close_hash, releases):
    """Publicly verify who authorised reconstruction, without seeing shares."""
    body = manifest["payload"]  # caller must validate the manifest signature first
    members = {m["custodian"]: m for m in body["custodians"]}
    used = set()
    for record in releases:
        signer = record["signer"]
        if signer not in members or signer in used:
            raise ValueError("unknown or duplicate releasing custodian")
        payload = verify_signed(record, signer)
        if (payload["type"] != "pol2-release-v1" or payload["election_id"] != body["election_id"]
                or payload["manifest_hash"] != sha256(canonical(manifest))
                or payload["close_hash"] != close_hash or payload["processor"] != body["processor"]
                or payload["index"] != members[signer]["index"]):
            raise ValueError("release is bound to a different election or close")
        used.add(signer)
    if len(used) < body["k"]:
        raise ValueError("insufficient independent custodian releases")


def decrypt_export(state, manifest, export, checkpoint, releases):
    if state["role"] != "processor":
        raise ValueError("only the key application may reconstruct the key")
    body, cp = verify_close(export, manifest, state, checkpoint)
    private = PrivateKey(bytes.fromhex(state["encryption_secret"]))
    if private.public_key.encode(HexEncoder).decode() != body["processor"]:
        raise ValueError("wrong key application")
    verify_release_attestations(manifest, cp["log_hash"], releases)
    members = {m["custodian"]: m for m in body["custodians"]}
    shares = []
    used = set()
    for record in releases:
        signer = record["signer"]
        if signer not in members or signer in used:
            raise ValueError("unknown or duplicate releasing custodian")
        payload = verify_signed(record, signer)
        if (payload["type"] != "pol2-release-v1" or payload["election_id"] != body["election_id"]
                or payload["manifest_hash"] != sha256(canonical(manifest))
                or payload["close_hash"] != cp["log_hash"]
                or payload["processor"] != body["processor"]
                or payload["index"] != members[signer]["index"]):
            raise ValueError("release is bound to a different election or close")
        packet = json.loads(SealedBox(private).decrypt(bytes.fromhex(payload["sealed_share"])))
        if (packet["election_id"] != body["election_id"]
                or packet["config_hash"] != body["config_hash"]
                or packet["public_key"] != body["public_key"]
                or packet["share"]["index"] != payload["index"]):
            raise ValueError("released share packet mismatch")
        shares.append(KeyShare.from_dict(packet["share"]))
        used.add(signer)
    sk = recover_private_key(shares, body["k"])
    if sk.public_key.encode(HexEncoder).decode() != body["public_key"]:
        raise ValueError("reconstructed key differs from election key")
    log = json.loads(json.dumps(export["log"]))
    positions = {e["payload"]["envelope_hash"]: e["seq"] for e in log if e["kind"] == "ballot"}
    final = {}
    for raw in sorted(export["envelopes"], key=lambda e: positions[Envelope.from_dict(e).envelope_hash]):
        env = Envelope.from_dict(raw)
        final[env.agent_id] = env
    valid, invalid = {}, {}
    choices = log[0]["payload"]["config"]["choices"]
    for aid, env in final.items():
        try:
            ballot = Ballot.from_bytes(unseal(env.sealed, sk))
        except Exception as exc:
            invalid[aid] = f"undecryptable: {exc}"
            continue
        if ballot.agent_id != aid or ballot.election_id != body["election_id"]:
            invalid[aid] = "inner/outer envelope mismatch"
        elif ballot.choice not in choices:
            invalid[aid] = f"choice not in list: {ballot.choice!r}"
        else:
            valid[aid] = {"choice": ballot.choice, "rationale": ballot.rationale, "nonce": ballot.nonce}
    # The election key is intentionally public after closing, as in local mode.
    payload = {"published_private_key": bytes(sk).hex(), "shares_used": [s.index for s in shares[:body['k']]],
               "valid": valid, "invalid": invalid, "custodian_releases": releases,
               "closing_checkpoint": checkpoint}
    entry = LogEntry(len(log), "decrypt", payload, log[-1]["hash"], time.time())
    entry.hash = entry.compute_hash()
    log.append(entry.to_dict())
    return {"log": log, "envelopes": export["envelopes"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["init", "ceremony", "release", "decrypt"])
    parser.add_argument("--state")
    parser.add_argument("--role", choices=["custodian", "processor"])
    parser.add_argument("--organiser")
    parser.add_argument("--witness")
    parser.add_argument("--processor")
    parser.add_argument("--public-output")
    parser.add_argument("--config")
    parser.add_argument("--custodians", nargs="+")
    parser.add_argument("--organiser-secret")
    parser.add_argument("--manifest")
    parser.add_argument("--export")
    parser.add_argument("--checkpoint")
    parser.add_argument("--releases", nargs="+")
    parser.add_argument("--output")
    args = parser.parse_args()
    try:
        if args.action == "init":
            if not all((args.state, args.role, args.organiser, args.witness, args.public_output)):
                raise ValueError("init needs --state --role --organiser --witness --public-output")
            state, public = initialise(args.role, args.organiser, args.witness, args.processor)
            write_new(args.state, state, secret=True)
            write_new(args.public_output, public)
        elif args.action == "ceremony":
            if not all((args.config, args.custodians, args.processor, args.organiser_secret, args.output)):
                raise ValueError("ceremony needs config, custodians, processor, organiser secret and output")
            key = SigningKey(bytes.fromhex(read_json(args.organiser_secret)["signing_key"]))
            result = ceremony(read_json(args.config), [read_json(p) for p in args.custodians], args.processor, key)
            write_new(args.output, result)
        else:
            if not all((args.state, args.manifest, args.export, args.checkpoint, args.output)):
                raise ValueError("release/decrypt need state, manifest, export, checkpoint and output")
            params = (read_json(args.state), read_json(args.manifest), read_json(args.export), read_json(args.checkpoint))
            if args.action == "release":
                result = release(*params)
            else:
                if not args.releases:
                    raise ValueError("decrypt needs --releases")
                result = decrypt_export(*params, [read_json(p) for p in args.releases])
            write_new(args.output, result)
    except (ValueError, KeyError, TypeError, OSError, IndexError, StopIteration, CryptoError) as exc:
        parser.exit(1, f"custody operation failed: {exc}\n")


if __name__ == "__main__":
    main()
