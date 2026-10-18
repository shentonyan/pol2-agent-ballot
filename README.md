<div align="center">

# pol2-agent-ballot

**Sealed, auditable ballots for AI agents.**<br>
A voting protocol modelled on Estonia's IVXV that keeps each agent's vote
independent until the close, then lets anyone recompute the result.

[![CI](https://github.com/shentonyan/pol2-agent-ballot/actions/workflows/ci.yml/badge.svg)](https://github.com/shentonyan/pol2-agent-ballot/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-2459C9.svg)](LICENSE)
![Python ≥ 3.10](https://img.shields.io/badge/python-%E2%89%A53.10-2459C9)
[![Live demo](https://img.shields.io/badge/demo-live-1F8A5B)](https://shentonyan.github.io/pol2-agent-ballot/)

[**Live demo**](https://shentonyan.github.io/pol2-agent-ballot/) ·
[Design](docs/design.md) ·
[Related work](docs/related-work.md) ·
[中文说明](README.zh-CN.md)

</div>

<p align="center">
  <img src="docs/assets/protocol.svg" alt="Protocol overview: organiser, agents, collector ledger, custodians, auditor" width="900">
</p>

## Why

When several AI agents decide together whether a task, proposal or deployment
may go ahead, two things decide whether the group is better than one agent:

- **Independence.** A majority of independent, better-than-chance voters is
  more reliable than any one of them, and more so as voters are added
  (Condorcet's jury theorem). If agents see earlier votes, later agents tend
  to follow them and stop contributing their own evidence (an information
  cascade). Language models are known to defer to visible peers.
- **Verifiability.** If the result is computed inside an orchestrator, nobody
  else can check it. Governance processes such as
  [PoL2 / NaturalDAO](https://github.com/naturaldao/NaturalDAO) require a
  decision chain that third parties can inspect.

Electronic voting already solves both problems for people. This repository
adapts the structure of [IVXV](https://github.com/valimised/ivxv), the system
behind Estonia's legally binding internet elections, to agent voters, and
changes one thing on purpose: ballots are **secret until the close and
public afterwards**, so every decision carries each agent's reasons.

## At a glance

| Question | Answer from this repository |
|---|---|
| How much does sealing protect a group from cascades? | 9 agents, each right 70% of the time: sealed **0.903** vs open sequential **0.843** collective accuracy (30,000 audited runs each; exact values 0.901 and 0.840). In the open condition 22.6% of ballots go against the agent's own evidence. |
| Does adding agents help? | Sealed: 0.78 → 0.98 from 3 to 25 agents. Open: flat at about 0.84 from 9 agents on. |
| Can consensus be run on the same protocol? | Yes: repeated sealed rounds with disclosure in between reach unanimity in at most two rounds, with the accuracy of a single sealed vote. Each agent that never changes position cuts the chance of agreement (1.00, 0.71, 0.50, 0.35, 0.23 for 0–4 holdouts). |
| Can a dishonest collector change the result? | Not without detection: altered tallies, dropped envelopes, forged ballots and misreported decryptions are caught by an auditor that only sees the exported record. |
| Does any of this hold for real language-model agents? | Not yet tested. [`experiments/llm_herding.py`](experiments/llm_herding.py) runs the pre-registered test against any OpenAI-compatible endpoint, including a local Ollama. |

Every simulated number has an exact counterpart in
[`theory.py`](src/pol2_agent_ballot/theory.py); the tests check them against
each other and against brute-force enumeration.

> **Status: research prototype (v0.3).** Not reviewed for production use.
> No IVXV code is included; IVXV is licensed CC BY-NC-ND and only its
> published design is used as a reference.

## Contents

[How it works](#how-it-works) ·
[Installation](#installation) ·
[Usage](#usage) ·
[Experiments](#experiments) ·
[Related work](#related-work) ·
[Security model](#security-model-and-limitations) ·
[Layout](#repository-layout) ·
[Citation](#citation)

## How it works

```mermaid
sequenceDiagram
    autonumber
    participant O as Organiser
    participant C as Custodians (2-of-3)
    participant A as Agent
    participant L as Collector ledger
    participant X as Auditor
    O->>L: signed configuration + signed roster
    O->>C: election public key; private key split into shares, then discarded
    A->>A: decide (choice, rationale) from private evidence
    A->>L: envelope = Sign_agent(Seal_pk(ballot))
    L-->>A: receipt (ledger position, chain hash, collector signature)
    Note over A,L: re-voting allowed; the latest envelope counts
    O->>L: close
    C->>L: two shares → private key, checked against public key, published
    L->>L: decrypt final envelopes, tally, apply rule
    X->>L: recompute everything from the exported record
```

| IVXV | This implementation | Difference |
|---|---|---|
| Signed voter list | Organiser-signed roster of Ed25519 keys | No PKI; the organiser key is the trust anchor |
| Inner envelope (ElGamal) | libsodium sealed box to the election key | No homomorphic property needed |
| Outer envelope (BDOC signature) | Ed25519 over election, agent, ciphertext hash, time | — |
| Registration receipt (OCSP + RFC 3161) | Collector-signed receipt with ledger position and chain hash | — |
| Re-voting | Latest envelope per agent counts | — |
| Threshold key on smart cards | Shamir k-of-n over GF(2²⁵⁵ − 19) | — |
| Mix-net + zero-knowledge decryption proofs | Private key published after the close | Secrecy ends at the close |
| Auditor application | `Auditor` on the exported record | — |

An event (a task, a welfare proposal, a deployment) moves through stages
`proposed → deliberation → voting → approved → executed → reviewed`. Each
step is one ballot. The event advances only if the decision rule is met, the
winner is the "advance" choice, the proposal passed the EAP check, and the
audit passed. Details: [docs/design.md](docs/design.md). Reading notes on the
IVXV sources: [docs/ivxv-notes.md](docs/ivxv-notes.md).

## Installation

```bash
git clone https://github.com/shentonyan/pol2-agent-ballot.git
cd pol2-agent-ballot
pip install -e ".[dev,experiments]"
```

Python 3.10 or newer. The only runtime dependency is
[PyNaCl](https://github.com/pyca/pynacl); experiments also use matplotlib.

## Usage

**One stage transition**

```python
from pol2_agent_ballot.agents import LLMAgent, Proposal
from pol2_agent_ballot.event import Event, run_transition_ballot
from pol2_agent_ballot.identity import AgentIdentity

agents = [LLMAgent(AgentIdentity.generate(f"agent-{i}"), complete=my_llm) for i in range(5)]
event = Event("TASK-7", "Ship the memory-registry adapter")
proposal = Proposal("P-7-1", "Move TASK-7 to voting", "…", choices=["advance", "hold"])

run = run_transition_ballot(event, proposal, AgentIdentity.generate("organiser"), agents,
                            rule="supermajority", quorum=0.6)

run.tally.winner   # 'advance' | 'hold' | None
run.audit.ok       # True only if every audit check passed
event.stage        # moved on only if decided, winner is choices[0], EAP and audit passed
event.to_dict()    # complete record: ledger, envelopes, tally, audit, human queries
```

`my_llm` is any function from a prompt string to a completion string. The
repository contains no API keys and depends on no model provider.

**Consensus**

```python
from pol2_agent_ballot.consensus import run_consensus

result = run_consensus(event, proposal, organiser, agents, rule="unanimous", max_rounds=5)
result.reached, result.n_rounds, [r.changed_minds for r in result.rounds]
```

Each round is a full sealed election with its own key and audit; between
rounds every ballot and rationale is disclosed to all agents. Rounds that do
not meet the rule are kept as transitions with `advanced=False`, so the
deliberation is part of the record.

**Human oversight.** PoL2 gives people the right to suggest and to query,
not to vote. `Event.add_query(who, text)` records a query and
`Event.answer_query(id, answer, by)` records the agents' explanation.

**Exact theory**

```python
from pol2_agent_ballot.theory import condorcet, cascade, consensus
condorcet(9, 0.7)            # 0.9012  sealed majority accuracy
cascade(9, 0.7, lead=2)      # {'accuracy': 0.8403, 'conformity_rate': 0.2264}
consensus(9, 0.7, stubborn=2)  # {'reached': 0.5077, ...}
```

## Experiments

All three experiments share the same setup: each agent receives private
evidence that points to the right answer with probability *p*. Intervals are
Wilson 95%; dashed lines in the figures are exact values from `theory.py`.

### 1 · Sealed versus open sequential voting

`python experiments/sealed_vs_open.py --trials 30000 --sweep --sweep-trials 10000`

Open condition: agents vote one after another, see the running tally, and
follow it once it leads by two, the point at which a Bayesian agent's own
signal is outweighed in the cascade model of Bikhchandani, Hirshleifer and
Welch (1992).

| Condition | Simulated | 95% CI | Exact | Ballots against own evidence |
|---|---|---|---|---|
| Sealed | 0.903 | 0.900 – 0.907 | 0.901 | 0.0% |
| Open sequential | 0.843 | 0.839 – 0.847 | 0.840 | 22.6% (exact 22.6%) |

![Experiment 1](experiments/figures/headline.png)
![Experiment 1, sweeps](experiments/figures/sweep.png)

Sealed voting follows the Condorcet curve; open voting stops improving after
about nine agents because later agents add no information once a cascade
starts. Across 32 sweep points (10,000 runs each, independent seeds) the
exact value lies inside the 95% interval at 29, in line with the 30 or so
expected.

### 2 · Consensus through sealed rounds

`python experiments/consensus_rounds.py --trials 3000 --plot`

| Agents that never update | Agreement reached | Exact | Mean rounds | Accuracy when reached |
|---|---|---|---|---|
| 0 | 1.000 | 1.000 | 1.96 | 0.904 |
| 1 | 0.709 | 0.718 | 2.83 | 0.926 |
| 2 | 0.502 | 0.508 | 3.45 | 0.942 |
| 3 | 0.353 | 0.355 | 3.90 | 0.955 |
| 4 | 0.231 | 0.246 | 4.26 | 0.986 |

![Experiment 2](experiments/figures/consensus.png)

Agreement comes in round 1 or 2 or not at all: after round 2 no ballot
changes. With no holdouts, unanimity has exactly the accuracy of one sealed
majority vote, consistent with the finding of Choi, Zhu and Li (2025) that
voting carries most of the gains attributed to multi-agent debate. The rising
accuracy-when-reached column is a selection effect (agreement happens only
when the holdouts already agree with the majority), not a benefit of holding
out.

### 3 · Language-model agents (runnable, not yet run)

```powershell
# offline check of the pipeline
python experiments/llm_herding.py --backend mock --proposals 40
# a local model through Ollama's OpenAI-compatible endpoint
$env:LLM_BASE_URL = "http://localhost:11434/v1"; $env:LLM_MODEL = "qwen2.5:7b-instruct"
python experiments/llm_herding.py --backend openai --proposals 40
```

Release-review proposals with known ground truth; each agent gets one private
review result that is wrong with probability 1 − *p*; the same evidence is
used in the sealed and open conditions, and the script reports the paired
difference with a bootstrap interval. Design and honesty clauses:
[experiments/llm_herding_protocol.md](experiments/llm_herding_protocol.md).

## Related work

[docs/related-work.md](docs/related-work.md) places the project against 42
references in three literatures, with BibTeX in
[docs/references.bib](docs/references.bib).
[docs/related-repos.md](docs/related-repos.md) lists 25 related open-source
projects. The closest are:

| | Shares with this project | Differs in |
|---|---|---|
| [IVXV](https://github.com/valimised/ivxv) (Heiberg et al. 2016) | Double envelope, receipts, re-voting, threshold key, auditor | Human voters; ballots stay secret |
| [Shutter shielded voting](https://github.com/shutter-network/rolling-shutter) | Threshold-sealed DAO votes revealed at the close, to stop bandwagoning | No per-ballot rationale or independent audit record |
| [ElectionGuard](https://github.com/Election-Tech-Initiative/electionguard-python) (Benaloh et al. 2024) | Guardian key ceremony, verifiable decryption | Lasting secrecy via proofs; heavier cryptography |
| [Voting or Consensus?](https://github.com/lkaesberg/decision-protocols) (Kaesberg et al. 2025) | Compares decision protocols for LLM agents | Aggregation inside the orchestrator; nothing sealed or auditable |
| [BenchForm](https://github.com/Zhiyuan-Weng/BenchForm) (Weng et al. 2025) | Measures LLM conformity to visible majorities | A benchmark; this project is a countermeasure |

## Security model and limitations

**Detected by the auditor:** forged, dropped or injected envelopes; ballots
from agents not on the roster; edits to the configuration or roster;
misreported decryptions; a tally that does not match the ballots; a
misapplied rule; a broken ledger chain. Each case has a test.

**Not addressed:**

- **One organiser key** signs the roster and configuration. A deployment
  should anchor both in a multi-party or on-chain registry.
- **Custodian collusion.** Any *k* custodians together can decrypt early, as
  with IVXV's smart cards.
- **No lasting anonymity,** by design. Where agents could face retaliation
  (for example when rating each other), use a mix-net or ElectionGuard.
- **Correlated agents.** Sealing removes the cascade, not correlation from a
  shared base model (Ladha 1992); the experiment-1 gains are an upper bound.
- **EAP checker.** `KeywordEAP` is a placeholder and does not assess 恨语
  (hate-language in the PoL2 sense).
- **Browser demo** uses tweetnacl with SHA-512 in place of BLAKE2b for the
  sealed-box nonce, so its ciphertexts are not interchangeable with PyNaCl's.

PoL2's manuscript says NaturalDAO's autonomy needs no human voting. Here the
voters are agents, and people keep the suggestion and query rights the
manuscript gives them. Whether PoL2 should name majority voting or consensus
as its rule is left to its editors; both run on this protocol.

## Repository layout

```
src/pol2_agent_ballot/
  identity.py    agent keys, signed roster
  crypto.py      sealed box, Shamir k-of-n key sharing
  ballot.py      ballot and double envelope
  election.py    collector, receipts, individual verification, re-voting, decryption
  tally.py       decision rules and quorum
  verify.py      independent auditor
  consensus.py   multi-round consensus
  theory.py      exact results for all experiments
  event.py       event stages, transition ballots, human queries
  agents.py      scripted, conformist, deliberative and LLM agents
  eap.py         Ethical Alignment Protocol hook
experiments/     experiment scripts, results/ (JSON), figures/ (PNG)
site/app.html    source of the live demo; built to docs/index.html
docs/            design, related work, references.bib, related repos, IVXV notes
tests/           protocol, tampering, consensus, theory (24 tests)
```

## Development

```bash
python -m pytest -q                 # 24 tests
python experiments/make_figures.py  # redraw figures from results/
python scripts/build_site.py        # rebuild docs/index.html
```

CI runs the tests on Python 3.10 and 3.12 and checks that the demo page is
up to date. The `pages` workflow publishes `docs/` to GitHub Pages.

## Citation

Please cite with the metadata in [CITATION.cff](CITATION.cff).

## License and acknowledgements

MIT, see [LICENSE](LICENSE). Protocol structure after the published design of
IVXV by the Estonian State Electoral Office and Cybernetica AS (CC BY-NC-ND
3.0); no IVXV source code is included. Governance context from
[naturaldao/NaturalDAO](https://github.com/naturaldao/NaturalDAO).
