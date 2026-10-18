"""Independent audit.

IVXV's auditor is a separate application that never trusts the collector,
processor or key application: it re-checks the shuffle proof, the decryption
proofs and the integrity of the processor logs against the ballot boxes.

Our `Auditor` takes only the *exported* log and envelopes (what a third party
would be given) and re-derives everything:

1. the log is a valid hash chain;
2. the config and the roster carry valid organiser signatures;
3. every accepted envelope has a valid agent signature, the agent was on the
   roster, and the envelope hash in the log matches the envelope;
4. the re-vote rule was applied (latest envelope per agent counted);
5. with the published private key, every final envelope decrypts to the
   ballot the decrypt record claims, and invalid ballots are really invalid;
6. the tally record equals a fresh tally of the decrypted ballots and the
   decision rule was applied correctly.

Because the private key is published at close, step 5 replaces IVXV's
zero-knowledge decryption proof. It gives the same guarantee (nobody can
misreport a decryption) at the cost of ballot secrecy after close — which is
what PoL2's transparency principle asks for anyway.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from nacl.encoding import HexEncoder
from nacl.public import PrivateKey, PublicKey
from nacl.signing import VerifyKey

from .ballot import Ballot, Envelope, canonical, sha256
from .crypto import unseal
from .identity import AgentRegistry


@dataclass
class AuditReport:
    ok: bool = True
    checks: List[str] = field(default_factory=list)
    failures: List[str] = field(default_factory=list)

    def passed(self, msg: str) -> None:
        self.checks.append(msg)

    def failed(self, msg: str) -> None:
        self.ok = False
        self.failures.append(msg)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "checks": self.checks, "failures": self.failures}


class Auditor:
    """Verify an exported election with no access to live objects."""

    def __init__(self, log: List[dict], envelopes: List[dict], *,
                 trusted_organiser: Optional[str] = None,
                 receipts: Optional[List[dict]] = None,
                 checkpoints: Optional[List[dict]] = None,
                 trusted_witnesses: Optional[List[str]] = None,
                 require_complete: bool = False):
        self.log = log
        self.envelopes = envelopes
        self.trusted_organiser = trusted_organiser
        self.receipts = receipts or []
        self.checkpoints = checkpoints or []
        self.trusted_witnesses = trusted_witnesses or []
        self.require_complete = require_complete

    def _entries(self, kind: str) -> List[dict]:
        return [e for e in self.log if e["kind"] == kind]

    def run(self) -> AuditReport:
        """Malformed evidence fails closed; unfinished elections remain auditable."""
        r = AuditReport()
        try:
            return self._run(r)
        except (KeyError, IndexError, TypeError, ValueError, AttributeError, OverflowError) as exc:
            r.failed(f"malformed audit record: {type(exc).__name__}: {exc}")
            return r

    def _run(self, r: AuditReport) -> AuditReport:
        envelopes = [Envelope.from_dict(e) for e in self.envelopes]
        # Singleton records and lifecycle order must be checked independently
        # of hashes: a collector can recompute an unsigned hash chain.
        kinds = [e["kind"] for e in self.log]
        for kind in ("config", "registry"):
            if kinds.count(kind) != 1:
                r.failed(f"expected exactly one {kind} record")
        for kind in ("key_ceremony", "open", "close", "decrypt", "tally", "eap"):
            if kinds.count(kind) > 1:
                r.failed(f"duplicate {kind} record")
        phase = "setup"
        for pos, e in enumerate(self.log):
            kind = e["kind"]
            if e["seq"] != pos:
                r.failed(f"non-contiguous log sequence at position {pos}")
            if pos < 2:
                if kind != ("config", "registry")[pos]:
                    r.failed("config and registry must start the log")
                continue
            if kind == "key_ceremony" and phase == "setup":
                phase = "keyed"
            elif kind == "open" and phase == "keyed":
                phase = "open"
            elif kind == "ballot" and phase == "open":
                pass
            elif kind == "close" and phase == "open":
                phase = "closed"
            elif kind == "decrypt" and phase == "closed":
                phase = "decrypted"
            elif kind == "tally" and phase == "decrypted":
                phase = "tallied"
            elif kind == "eap" and phase == "tallied":
                pass
            else:
                r.failed(f"unexpected {kind!r} record during {phase}")
        if r.failures:
            return r
        # 1. hash chain
        prev = "0" * 64
        for e in self.log:
            body = canonical({"seq": e["seq"], "kind": e["kind"], "payload": e["payload"], "prev_hash": e["prev_hash"], "ts": e["ts"]})
            if e["prev_hash"] != prev or sha256(body) != e["hash"]:
                r.failed(f"log chain broken at seq {e['seq']}")
                return r
            prev = e["hash"]
        r.passed(f"hash chain intact ({len(self.log)} entries)")

        # 2. config + roster signatures
        cfg_e = self._entries("config")[0]
        if (self.trusted_organiser is not None
                and cfg_e["payload"]["organiser"] != self.trusted_organiser):
            r.failed("organiser does not match independently trusted key")
            return r
        if self.require_complete and not self._entries("tally"):
            r.failed("complete audit requires a tally record")
            return r
        organiser = VerifyKey(cfg_e["payload"]["organiser"].encode(), encoder=HexEncoder)
        try:
            organiser.verify(canonical(cfg_e["payload"]["config"]), bytes.fromhex(cfg_e["payload"]["signature"]))
            r.passed("election config signed by organiser")
        except Exception:
            r.failed("election config signature invalid")
        cfg = cfg_e["payload"]["config"]
        from .election import ElectionConfig

        ElectionConfig(**cfg)  # apply the same configuration validation as the collector
        registry = AgentRegistry.from_dict(self._entries("registry")[0]["payload"])
        if registry.verify() and registry.organiser_key == organiser:
            r.passed(f"roster signed by organiser ({len(registry.ids())} eligible agents)")
        else:
            r.failed("roster signature invalid or signed by someone else")

        ceremony_records = self._entries("key_ceremony")
        if ceremony_records and "custody_manifest" in ceremony_records[0]["payload"]:
            from .custody import validate_manifest

            ceremony = ceremony_records[0]["payload"]
            manifest_body = validate_manifest(ceremony["custody_manifest"], cfg, cfg_e["payload"]["organiser"])
            if any(ceremony[k] != manifest_body[k] for k in ("public_key", "k", "n")):
                r.failed("key ceremony does not match signed custody manifest")
                return r

        # 3. envelopes vs log
        ballot_entries = self._entries("ballot")
        by_hash = {e["payload"]["envelope_hash"]: e for e in ballot_entries}
        if len(by_hash) != len(ballot_entries):
            r.failed("duplicate envelope hashes in log")
        seen: Dict[str, List[Envelope]] = {}
        exported_hashes = [env.envelope_hash for env in envelopes]
        if len(set(exported_hashes)) != len(exported_hashes):
            r.failed("duplicate exported envelopes")
        if set(exported_hashes) != set(by_hash):
            r.failed("exported envelope hashes do not match registered ballots")
        for env in envelopes:
            if env.election_id != cfg["election_id"]:
                r.failed("envelope belongs to a different election")
                continue
            if env.envelope_hash not in by_hash:
                r.failed(f"envelope {env.envelope_hash[:12]} not registered in log")
                continue
            if not registry.is_eligible(env.agent_id):
                r.failed(f"envelope from non-roster agent {env.agent_id[:12]}")
                continue
            try:
                registry.verify_key_of(env.agent_id).verify(env.signed_bytes(), env.signature)
            except Exception:
                r.failed(f"bad signature on envelope {env.envelope_hash[:12]}")
                continue
            if by_hash[env.envelope_hash]["payload"]["sealed_sha256"] != sha256(env.sealed):
                r.failed(f"sealed content of {env.envelope_hash[:12]} differs from registered hash")
            if by_hash[env.envelope_hash]["payload"]["agent_id"] != env.agent_id:
                r.failed("registered agent differs from envelope signer")
            entry = by_hash[env.envelope_hash]
            receipt = env.receipt
            if not isinstance(receipt, dict):
                r.failed(f"missing receipt for {env.envelope_hash[:12]}")
            else:
                signed_receipt = {
                    "envelope_hash": env.envelope_hash,
                    "log_seq": entry["seq"], "log_hash": entry["hash"],
                }
                if (receipt.get("election_id") != cfg["election_id"]
                        or any(receipt.get(k) != v for k, v in signed_receipt.items())):
                    r.failed("receipt does not match registered envelope and log position")
                try:
                    organiser.verify(canonical(signed_receipt),
                                     bytes.fromhex(receipt["collector_signature"]))
                except Exception:
                    r.failed("collector receipt signature invalid")
            seen.setdefault(env.agent_id, []).append(env)
        if len(envelopes) != len(ballot_entries):
            r.failed(f"{len(ballot_entries)} ballots in log but {len(envelopes)} envelopes exported")
        if not r.failures:
            r.passed(f"all {len(envelopes)} envelopes registered, eligible, correctly signed")

        # External copies survive deletion of both an envelope and its log entry.
        from .evidence import verify_external

        try:
            verify_external(self.log, self.envelopes, cfg_e["payload"]["organiser"],
                            self.receipts, self.checkpoints, self.trusted_witnesses)
            if self.receipts or self.checkpoints:
                r.passed("independent receipts and trusted checkpoints match")
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            r.failed(f"external evidence mismatch: {exc}")
            return r

        # 4. re-vote rule: final = latest by log sequence
        final: Dict[str, Envelope] = {}
        for aid, envs in seen.items():
            envs.sort(key=lambda e: by_hash[e.envelope_hash]["seq"])
            final[aid] = envs[-1]
        r.passed(f"re-vote rule applied: {len(final)} final envelopes from {len(envelopes)} accepted")

        # 5. re-decrypt everything
        dec = self._entries("decrypt")
        if not dec:
            r.passed("no decrypt record yet (election still open/closed)")
            return r
        dec = dec[0]["payload"]
        if ceremony_records and "custody_manifest" in ceremony_records[0]["payload"]:
            from .custody import verify_release_attestations
            from .evidence import verify_signed

            manifest = ceremony_records[0]["payload"]["custody_manifest"]
            close_entry = self._entries("close")[0]
            verify_release_attestations(manifest, close_entry["hash"], dec["custodian_releases"])
            if dec["shares_used"] != [c["payload"]["index"] for c in dec["custodian_releases"][:manifest_body["k"]]]:
                r.failed("share indices differ from signed custodian releases")
                return r
            cp = verify_signed(dec["closing_checkpoint"], manifest_body["witness"])
            verify_external(self.log, self.envelopes, cfg_e["payload"]["organiser"], [],
                            [dec["closing_checkpoint"]], [manifest_body["witness"]])
            if cp["phase"] != "close" or cp["seq"] != close_entry["seq"]:
                r.failed("custodians did not attest this closing checkpoint")
                return r
        sk = PrivateKey(bytes.fromhex(dec["published_private_key"]))
        pk_hex = self._entries("key_ceremony")[0]["payload"]["public_key"]
        if sk.public_key != PublicKey(pk_hex.encode(), encoder=HexEncoder):
            r.failed("published private key does not match election public key")
            return r
        r.passed("published private key matches election public key")
        recomputed: Dict[str, Ballot] = {}
        invalid: Dict[str, str] = {}
        for aid, env in final.items():
            try:
                b = Ballot.from_bytes(unseal(env.sealed, sk))
            except Exception as e:
                invalid[aid] = f"undecryptable: {e}"
                continue
            if b.agent_id != aid or b.election_id != cfg["election_id"]:
                invalid[aid] = "inner/outer envelope mismatch"
            elif b.choice not in cfg["choices"]:
                invalid[aid] = f"choice not in list: {b.choice!r}"
            else:
                recomputed[aid] = b
        claimed_valid = dec["valid"]
        if set(claimed_valid) != set(recomputed):
            r.failed(f"decrypt record lists {sorted(claimed_valid)} valid, auditor finds {sorted(recomputed)}")
        else:
            for aid, b in recomputed.items():
                c = claimed_valid[aid]
                if (c["choice"], c["rationale"], c["nonce"]) != (b.choice, b.rationale, b.nonce):
                    r.failed(f"decrypt record misreports ballot of {aid[:12]}")
        if set(dec["invalid"]) != set(invalid):
            r.failed(f"invalid set differs: claimed {sorted(dec['invalid'])}, found {sorted(invalid)}")
        if not any("decrypt record" in f or "invalid set" in f for f in r.failures):
            r.passed(f"re-decrypted {len(recomputed)} ballots; decrypt record is faithful")

        # 6. tally
        tal = self._entries("tally")
        if not tal:
            return r
        tal = tal[0]["payload"]
        counts = Counter({c: 0 for c in cfg["choices"]})
        for b in recomputed.values():
            counts[b.choice] += 1
        if dict(counts) != tal["counts"]:
            r.failed(f"tally counts {tal['counts']} != recomputed {dict(counts)}")
        else:
            r.passed(f"tally counts match: {dict(counts)}")
        valid = sum(counts.values())
        eligible = len(registry.ids())
        quorum_met = eligible > 0 and valid / eligible >= cfg["quorum"]
        if quorum_met != tal["quorum_met"]:
            r.failed("quorum flag misreported")
        expected_decided = False
        expected_winner = None
        if valid and quorum_met:
            top, top_n = counts.most_common(1)[0]
            if sum(n == top_n for n in counts.values()) == 1:
                rule = cfg["rule"]
                expected_decided = {
                    "plurality": True,
                    "majority": top_n * 2 > valid,
                    "supermajority": top_n * 3 >= valid * 2,
                    "unanimous": top_n == valid,
                }[rule]
                expected_winner = top if expected_decided else None
        expected = {
            "eligible": eligible, "valid": valid, "invalid": len(invalid),
            "rule": cfg["rule"], "decided": expected_decided,
            "winner": expected_winner,
            "rationales": {
                c: {aid: b.rationale for aid, b in recomputed.items() if b.choice == c}
                for c in cfg["choices"]
            },
        }
        for name, value in expected.items():
            if name not in tal or tal[name] != value:
                r.failed(f"tally {name} misreported: expected {value!r}, got {tal.get(name)!r}")
        if not r.failures:
            r.passed(f"decision rule {cfg['rule']} correctly applied -> {expected_winner!r}")
        return r
