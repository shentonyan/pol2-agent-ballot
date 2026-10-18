# Distributed custody and independent evidence

This is an opt-in execution path alongside the original in-process experiment
API. It supplies separate CLI processes and pinned identities, not a production
network service or distributed key generation (DKG).

## Roles and trust

| Role | Holds before close | Receives / publishes |
|---|---|---|
| Offline dealer | Temporary election key and all Shamir shares, organiser signing key | Organiser-signed manifest with shares sealed separately to each custodian |
| Collector | Election public key, signed config / roster and ciphertexts | Signed receipts and a closed export; **no plaintext shares** |
| Custodian (one process per identity) | Its own encryption/signing keys | Its own sealed share packet; emits a signed release sealed to the pinned key application |
| Witness | Its signing key and independent evidence directory | Retains receipt copies; signs monotonic ledger checkpoints |
| Key application | Its own encryption key | Reconstructs from at least k distinct, signed releases after a witnessed close; publishes the election key and decrypt record |
| Auditor | Independently trusted organiser / witness public keys | Public export, retained receipts and checkpoints; needs **no private key** |

The manifest binds the election ID, full configuration hash (including creation
time), threshold, public key, custodian membership, witness and key application.
A release binds that manifest, exact closing hash, custodian index and recipient.
Changing an election, witness, key application or closing record invalidates it.
The exported decrypt record retains the signed releases and closing checkpoint,
so a third-party auditor checks the authorisation chain as well as the plaintext.

**Deployment boundary:** use different service accounts or machines with access
controls. A directory or subprocess on the same administrator-owned machine is
not an adversarial isolation boundary. Each custodian should receive only its
own private state; the collector must not have read access to those states, the
witness private state or evidence directory, the processor key, or dealer secrets.
The CLI uses owner-only file mode for private state on POSIX; configure Windows
ACLs separately. Exchange public files through an authenticated transport you
operate. There is deliberately no unauthenticated HTTP release endpoint.

The dealer is trusted during setup and must be taken offline afterwards. Python
cannot guarantee wiping secret memory; this is not DKG, an HSM or verifiable
secret sharing. Any k colluding custodians (or a compromised dealer) can defeat
pre-close secrecy. A malicious custodian can withhold or corrupt a share: the
key application fails rather than silently substituting a result. An operator
may retry with another valid quorum; automated recovery is not implemented.

## Reproducible demonstration

Install as in the root README, then choose a **new directory outside your repo**:

```bash
python examples/run_distributed_demo.py --directory ../ballot-demo-run
```

The demonstration starts actual CLI subprocesses for initialisation, the offline
dealer, witness persistence, each release and the key application. The collector
only loads public identities and encrypted ceremony output. It finishes with a
public-key-only independent audit and prints `audit_ok: true`,
`collector_retained_shares: 0` and the winning option. The directory contains
**demo private identities**: do not commit it or treat its identities as real
custodians. Rerunning against the same directory is rejected to avoid replacing
keys or retained evidence.

## Operating the processes separately

`ORG_KEY`, `WITNESS_KEY` and `PROCESSOR_KEY` below are public hex keys. Distribute
and verify them independently. In particular, do not obtain trust keys solely
from the collector's potentially malicious export.

1. **Witness account:** initialise its private identity, pinning the organiser:

   ```bash
   python -m pol2_agent_ballot.evidence init --state witness-secret.json --store evidence --organiser ORG_KEY
   ```

   Record the returned witness public key out of band. The witness operator
   determines when a close is legitimately allowed; this prototype does not
   enforce a scheduled deadline or supply a trusted clock.

2. **Key application account:** initialise a recipient and pin the witness:

   ```bash
   python -m pol2_agent_ballot.custody init --role processor --state processor-secret.json --organiser ORG_KEY --witness WITNESS_KEY --public-output processor-public.json
   ```

   `PROCESSOR_KEY` is `encryption_key` in that public JSON.

3. **Each custodian account:** create a different private identity:

   ```bash
   python -m pol2_agent_ballot.custody init --role custodian --state custodian-secret.json --organiser ORG_KEY --witness WITNESS_KEY --processor PROCESSOR_KEY --public-output custodian-public.json
   ```

   Give the dealer only the public files; never combine custodian private files
   on the collector. The manifest rejects duplicate identities and disagreements
   about pinned keys.

4. **Offline dealer:** use the exact signed `ElectionConfig` JSON and n public
   custodian records. The organiser private JSON has a `signing_key` hex seed
   for this project's Ed25519 organiser identity (not your Git SSH signing key).

   ```bash
   python -m pol2_agent_ballot.custody ceremony --config config.json --organiser-secret organiser-secret.json --custodians c1-public.json c2-public.json c3-public.json --processor PROCESSOR_KEY --output manifest.json
   ```

   The collector calls `election.install_custody_manifest(manifest)` instead of
   `generate_keys()`. The existing local mode remains available for experiments.

5. **Voter / witness channel:** send each receipt returned by `election.accept`
   to the independent witness immediately and wait for successful persistence:

   ```bash
   python -m pol2_agent_ballot.evidence receipt --state witness-secret.json --store evidence --input receipt.json
   ```

   Do not wait until the collector produces its final export: an omitted ballot
   can only be detected if a copy escaped the collector before it was omitted.
   This CLI saves receipts but does not automate transport or acknowledgements.

6. **Close and witness:** export `{"log": ..., "envelopes": ...}` after
   `election.close()`. On the witness account:

   ```bash
   python -m pol2_agent_ballot.evidence checkpoint --state witness-secret.json --store evidence --input closed.json --output close-checkpoint.json
   ```

   The witness checks the organiser, full ledger audit, all previously retained
   receipts and its earlier checkpoints before signing. It will not roll back
   or replace a conflicting checkpoint. Checkpoint signatures cover election,
   configuration, organiser, position, ledger hash and phase.

7. **Each consenting custodian:** release only against that witnessed close:

   ```bash
   python -m pol2_agent_ballot.custody release --state custodian-secret.json --manifest manifest.json --export closed.json --checkpoint close-checkpoint.json --output release.json
   ```

   Transfer k distinct release files to the key application. These files contain
   signed ciphertexts, not plaintext shares. An open election, wrong witness,
   changed manifest or substituted key application is rejected.

8. **Key application, then collector:**

   ```bash
   python -m pol2_agent_ballot.custody decrypt --state processor-secret.json --manifest manifest.json --export closed.json --checkpoint close-checkpoint.json --releases release-1.json release-2.json --output decrypted.json
   ```

   The collector calls `election.import_decryption(decrypted_export)`, which
   checks the exact closed prefix, envelopes, custody attestations and decryption
   before accepting it, then `tally(election)`. It never imports private shares.
   As in local mode, the election private key intentionally becomes public
   **after** close, ending ballot secrecy.

9. **Final witness and independent audit:** checkpoint the final tally export
   using the witness command above. Give the auditor a read-only copy of retained
   evidence (no private state). On an auditor account:

   ```bash
   python -m pol2_agent_ballot.evidence audit --store evidence --organiser ORG_KEY --witness WITNESS_KEY --input final.json
   ```

   Exit status is nonzero on failure. Final CLI audit requires a tally and a
   retained checkpoint matching the final head, rather than treating a partial
   open/closed log as a completed result.

## Storage and failure semantics

Evidence is kept per election under a SHA-256-derived directory, with independent
receipt files and checkpoints indexed by sequence. Exclusive creation prevents
accidental replacement. Re-saving identical public evidence is idempotent;
private identity files are never overwritten. Writes are flushed and fsynced,
including parent directory fsync where supported. Windows directory fsync is not
available here; filesystem/hardware durability still depends on deployment.

Concurrent checkpoint writers use an exclusive lock and fail closed. After a
crash, an operator must confirm that the writer is dead before removing its
stale lock. Corrupt or partially written evidence is rejected, not skipped.
There is no promise of automatic repair, distributed consensus or WORM storage.

Independent receipts detect deletion of both a recorded envelope and its log
entry. A retained checkpoint detects truncation, replacement and rollback of its
prefix. Neither proves completeness for a ballot never acknowledged or retained,
nor detects a missing later checkpoint if the auditor is given only old copies.
The witness/evidence owner must preserve and supply the latest checkpoint via
an independent channel, with backups and access controls as appropriate.

For programmatic audit:

```python
report = Auditor(
    log, envelopes,
    trusted_organiser=organiser_public_key,
    receipts=retained_receipts,
    checkpoints=retained_checkpoints,
    trusted_witnesses=[witness_public_key],
    require_complete=True,
).run()
```

`Auditor(log, envelopes)` remains a compatibility path for internal consistency
checks. It does not independently establish organiser identity or completeness.
The standalone final audit adds the final-checkpoint requirement described above.

## Validation

`tests/test_distributed.py` covers actual separate-process execution, independent
persistence across reopen, refusal before close, k-of-n enforcement, duplicate
releases, trusted-key substitution, signed cross-election replay, tampering with
public custody authorisations, joint ballot/log omission, tail truncation,
checkpoint rollback, corrupt evidence, concurrent writers, and key-file overwrite
refusal. The original experiment tests and local demonstration still run.
