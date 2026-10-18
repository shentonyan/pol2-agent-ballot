"""Experiment 1 — does sealing ballots protect agent decisions from cascades?

Setup. N conformist agents each hold a private signal that is correct with
probability p. Under the *sealed* condition no agent sees any other vote
(the IVXV property). Under the *open* condition agents vote in sequence and
see the running tally, following it once it leads by `lead` votes.

Prediction (information-cascade theory, Bikhchandani–Hirshleifer–Welch 1992):
open sequential voting lets early wrong signals lock in; sealed voting keeps
the Condorcet jury property (accuracy rises with N when p > 0.5).

Honesty clause. This is a simulation of scripted agents with a hand-set
conformity rule. It shows what the protocol *protects against*, not that
real LLM agents behave this way. Whether LLM agents herd under open voting
is an empirical question — see experiments/llm_herding_protocol.md for the
pre-registered design, which has NOT been run.

Run:  python experiments/sealed_vs_open.py  [--trials 400] [--n 9] [--p 0.7]
"""
from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pol2_agent_ballot.agents import ConformistAgent, Proposal
from pol2_agent_ballot.event import Event, run_transition_ballot
from pol2_agent_ballot.identity import AgentIdentity


def one_trial(n: int, p: float, lead: int, sealed: bool, rng: random.Random) -> bool:
    organiser = AgentIdentity.generate("organiser")
    agents = [ConformistAgent(AgentIdentity.generate(f"a{i}"), truth="advance", accuracy=p, lead=lead, rng=random.Random(rng.random())) for i in range(n)]
    rng.shuffle(agents)
    ev = Event("EXP", "cascade test")
    prop = Proposal("P", "advance?", "", choices=["advance", "hold"])
    run = run_transition_ballot(ev, prop, organiser, agents, threshold_k=2, rule="majority", quorum=0.0, sealed=sealed)
    return run.tally.winner == "advance"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=400)
    ap.add_argument("--n", type=int, default=9)
    ap.add_argument("--p", type=float, default=0.7)
    ap.add_argument("--lead", type=int, default=2)
    ap.add_argument("--seed", type=int, default=2026)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    rows = []
    for sealed in (True, False):
        ok = sum(one_trial(a.n, a.p, a.lead, sealed, rng) for _ in range(a.trials))
        rows.append(("sealed" if sealed else "open", ok / a.trials))
    print(f"N={a.n} agents, private accuracy p={a.p}, conformity lead={a.lead}, trials={a.trials}")
    print(f"{'condition':<10}{'P(correct collective decision)':>32}")
    for name, acc in rows:
        print(f"{name:<10}{acc:>32.3f}")
    print("\nHonesty clause: scripted agents, hand-set conformity rule; no claim about real LLM agents or humans.")


if __name__ == "__main__":
    main()
