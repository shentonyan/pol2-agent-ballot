"""Durable external receipt copies and witness-signed ledger checkpoints.

Run the witness under a different account/host from the collector. Local file
separation alone is not an access-control boundary. Trust keys come from the
caller, never from the evidence being checked.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from nacl.encoding import HexEncoder
from nacl.signing import SigningKey, VerifyKey

from .ballot import canonical, sha256


def local_path(path):
    path = Path(os.path.abspath(path))
    if os.name == "nt" and not str(path).startswith("\\\\?\\"):
        raw = str(path)
        path = Path("\\\\?\\UNC\\" + raw[2:] if raw.startswith("\\\\") else "\\\\?\\" + raw)
    return path


def read_json(path):
    return json.loads(local_path(path).read_text(encoding="utf-8"))


def write_new(path, value, *, secret=False):
    """Exclusive creation: never silently replace a key or retained evidence."""
    path = local_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = canonical(value) + b"\n"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600 if secret else 0o644)
    except FileExistsError:
        if secret or path.read_bytes() != raw:
            raise ValueError(f"refusing to overwrite {path.name}") from None
        return
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    # Persist the directory entry where supported (Windows cannot fsync a dir).
    if os.name != "nt":
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def signed(payload, key):
    return {"payload": payload, "signer": key.verify_key.encode(HexEncoder).decode(),
            "signature": key.sign(canonical(payload)).signature.hex()}


def verify_signed(record, trusted_key):
    if record["signer"] != trusted_key:
        raise ValueError("unexpected signing key")
    try:
        VerifyKey(trusted_key, encoder=HexEncoder).verify(
            canonical(record["payload"]), bytes.fromhex(record["signature"]))
    except Exception as exc:
        raise ValueError("invalid evidence signature") from exc
    return record["payload"]


def verify_receipt(receipt, organiser):
    if type(receipt["log_seq"]) is not int or receipt["log_seq"] < 0:
        raise ValueError("invalid receipt position")
    payload = {k: receipt[k] for k in ("envelope_hash", "log_seq", "log_hash")}
    try:
        VerifyKey(organiser, encoder=HexEncoder).verify(
            canonical(payload), bytes.fromhex(receipt["collector_signature"]))
    except Exception as exc:
        raise ValueError("invalid collector receipt signature") from exc


def verify_external(log, envelopes, organiser, receipts, checkpoints, trusted_witnesses):
    cfg = log[0]["payload"]["config"]
    eid = cfg["election_id"]
    hashes = {e["payload"]["envelope_hash"] for e in log if e["kind"] == "ballot"}
    for receipt in receipts:
        verify_receipt(receipt, organiser)
        pos = receipt["log_seq"]
        if (receipt["election_id"] != eid or pos >= len(log)
                or log[pos]["hash"] != receipt["log_hash"]
                or log[pos]["kind"] != "ballot"
                or receipt["envelope_hash"] not in hashes
                or log[pos]["payload"]["envelope_hash"] != receipt["envelope_hash"]):
            raise ValueError("retained receipt is missing from or conflicts with ledger")
    for checkpoint in checkpoints:
        if checkpoint["signer"] not in trusted_witnesses:
            raise ValueError("checkpoint is not signed by a trusted witness")
        body = verify_signed(checkpoint, checkpoint["signer"])
        pos = body["seq"]
        if (body["type"] != "pol2-checkpoint-v1" or body["election_id"] != eid
                or body["organiser"] != organiser or body["config_hash"] != sha256(canonical(cfg))
                or type(pos) is not int or pos < 0 or pos >= len(log)
                or log[pos]["hash"] != body["log_hash"]
                or log[pos]["kind"] != body["phase"]):
            raise ValueError("checkpoint conflicts with ledger or ledger was truncated")


class WitnessStore:
    """A witness-owned directory, separate from the collector's export."""

    def __init__(self, root, organiser, key):
        self.root = local_path(root)
        self.organiser = organiser
        self.key = key
        self.public_key = key.verify_key.encode(HexEncoder).decode()

    def _dir(self, election_id):
        return self.root / sha256(election_id.encode())

    def receipts(self, election_id):
        return [read_json(p) for p in sorted((self._dir(election_id) / "receipts").glob("*.json"))]

    def checkpoints(self, election_id):
        return [read_json(p) for p in sorted((self._dir(election_id) / "checkpoints").glob("*.json"))]

    def save_receipt(self, receipt):
        verify_receipt(receipt, self.organiser)
        path = self._dir(receipt["election_id"]) / "receipts" / (receipt["envelope_hash"] + ".json")
        # Hash-based filenames must not accept traversal from untrusted receipts.
        if len(receipt["envelope_hash"]) != 64 or any(c not in "0123456789abcdef" for c in receipt["envelope_hash"]):
            raise ValueError("invalid envelope hash")
        write_new(path, receipt)

    def checkpoint(self, log, envelopes):
        from .verify import Auditor

        eid = log[0]["payload"]["config"]["election_id"]
        target = self._dir(eid) / "checkpoints"
        target.mkdir(parents=True, exist_ok=True)
        # Serialize concurrent checkpoint writers; stale locks fail closed.
        lock = target / "writer.lock"
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise ValueError("checkpoint writer already active; inspect stale lock before retry") from None
        try:
            os.close(fd)
            old = self.checkpoints(eid)
            report = Auditor(log, envelopes, trusted_organiser=self.organiser,
                             receipts=self.receipts(eid), checkpoints=old,
                             trusted_witnesses=[self.public_key]).run()
            if not report.ok:
                raise ValueError("witness refused ledger: " + "; ".join(report.failures))
            if old and len(log) - 1 < max(c["payload"]["seq"] for c in old):
                raise ValueError("checkpoint rollback")
            tail = log[-1]
            body = {"type": "pol2-checkpoint-v1", "election_id": eid,
                    "organiser": self.organiser, "config_hash": sha256(canonical(log[0]["payload"]["config"])),
                    "seq": tail["seq"], "log_hash": tail["hash"], "phase": tail["kind"]}
            record = signed(body, self.key)
            write_new(target / f"{tail['seq']:020d}.json", record)
            return record
        finally:
            lock.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["init", "receipt", "checkpoint", "audit"])
    parser.add_argument("--state", help="witness private identity file (not needed for audit)")
    parser.add_argument("--store", required=True, help="independent evidence directory")
    parser.add_argument("--organiser", help="trusted organiser public key; required at init/audit")
    parser.add_argument("--witness", help="trusted witness public key; required for audit")
    parser.add_argument("--input", help="receipt JSON or election export {log,envelopes}")
    parser.add_argument("--output", help="public checkpoint/report output")
    args = parser.parse_args()
    try:
        if args.action == "init":
            if not args.organiser or not args.state:
                raise ValueError("--organiser and --state are required")
            VerifyKey(args.organiser, encoder=HexEncoder)
            key = SigningKey.generate()
            write_new(args.state, {"organiser": args.organiser, "signing_key": key.encode().hex()}, secret=True)
            print(json.dumps({"witness": key.verify_key.encode(HexEncoder).decode()}))
            return
        if args.action == "audit":
            from .verify import Auditor

            if not all((args.organiser, args.witness, args.input)):
                raise ValueError("audit needs --organiser --witness --input (public keys only)")
            data = read_json(args.input)
            eid = data["log"][0]["payload"]["config"]["election_id"]
            base = local_path(args.store) / sha256(eid.encode())
            receipts = [read_json(p) for p in sorted((base / "receipts").glob("*.json"))]
            checkpoints = [read_json(p) for p in sorted((base / "checkpoints").glob("*.json"))]
            if not any(c.get("payload", {}).get("seq") == len(data["log"]) - 1
                       and c.get("payload", {}).get("log_hash") == data["log"][-1]["hash"]
                       and c.get("payload", {}).get("phase") in ("tally", "eap") for c in checkpoints):
                raise ValueError("independent final audit requires a retained final checkpoint")
            report = Auditor(data["log"], data["envelopes"], trusted_organiser=args.organiser,
                             receipts=receipts, checkpoints=checkpoints,
                             trusted_witnesses=[args.witness], require_complete=True).run()
            if args.output:
                write_new(args.output, report.to_dict())
            else:
                print(json.dumps(report.to_dict()))
            if not report.ok:
                raise SystemExit(1)
            return
        if not args.state:
            raise ValueError("--state is required for witness operations")
        state = read_json(args.state)
        store = WitnessStore(args.store, state["organiser"], SigningKey(bytes.fromhex(state["signing_key"])))
        if not args.input:
            raise ValueError("--input is required")
        data = read_json(args.input)
        if args.action == "receipt":
            store.save_receipt(data)
            result = {"saved": True}
        elif args.action == "checkpoint":
            result = store.checkpoint(data["log"], data["envelopes"])
        if args.output:
            write_new(args.output, result)
        else:
            print(json.dumps(result))
    except (ValueError, KeyError, TypeError, OSError, IndexError) as exc:
        parser.exit(1, f"evidence operation failed: {exc}\n")


if __name__ == "__main__":
    main()
