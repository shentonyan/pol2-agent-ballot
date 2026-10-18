# Changelog

## 0.2.0 — 2026-10-18

- Consensus mode: `consensus.run_consensus` runs repeated sealed rounds with
  full disclosure between rounds until the decision rule is met.
  `DeliberativeAgent` added.
- Experiment 2 (`experiments/consensus_rounds.py`), 3,000 runs per setting.
- Experiment 1 rerun with 30,000 runs per condition through the full
  protocol; Wilson intervals; parameter sweeps over N and p; figures.
  The 300-run figures in 0.1.0 (0.937 vs 0.820) were within sampling noise of
  the 30,000-run estimates (0.903 vs 0.843), which supersede them.
- Live demo (`site/app.html`, built to `docs/index.html` for GitHub Pages):
  step-through visualisation of the protocol in the browser, with sealed,
  open and consensus modes and a dishonest-collector mode.
- README rewritten; Chinese README split out; CITATION.cff.

## 0.1.0 — 2026-10-18

- Initial protocol: identities and signed roster, sealed double envelope,
  Shamir k-of-n election key, hash-chained ledger with receipts, individual
  verification, re-voting, decision rules, independent auditor, EAP hook,
  event stages and human queries, sealed-vs-open experiment.
