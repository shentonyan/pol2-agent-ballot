"""Experiment 3 — do language-model agents herd under open voting?

Runs the pre-registered design in experiments/llm_herding_protocol.md with
real language-model agents. Each agent receives a private, independently
corrupted piece of evidence about whether a release passed review, then votes
`advance` or `hold` through the full ballot protocol, either sealed or open
(sequential, seeing earlier votes). The same evidence draws are used in both
conditions, so the comparison is paired.

Backends (no SDK dependency; plain HTTP to an OpenAI-compatible endpoint):

  --backend mock     scripted ConformistAgents; checks the pipeline offline
  --backend openai   any OpenAI-compatible /v1/chat/completions endpoint
                     (OpenAI, vLLM, LM Studio, Ollama at http://localhost:11434/v1)

Environment for --backend openai:
  LLM_BASE_URL   e.g. http://localhost:11434/v1     (default)
  LLM_API_KEY    optional; never written to disk
  LLM_MODEL      e.g. qwen2.5:7b-instruct

Examples:
  python experiments/llm_herding.py --backend mock --proposals 40
  $env:LLM_MODEL="qwen2.5:7b-instruct"; python experiments/llm_herding.py --backend openai --proposals 40

Output: experiments/results/llm_herding/<run-id>.jsonl (one line per ballot)
and a summary printed at the end. Unparseable answers count as invalid
ballots and are reported, never re-prompted (see the protocol's honesty
clauses).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pol2_agent_ballot.agents import ConformistAgent, LLMAgent, Proposal
from pol2_agent_ballot.event import Event, run_transition_ballot
from pol2_agent_ballot.identity import AgentIdentity

HERE = Path(__file__).resolve().parent
OUT = HERE / "results" / "llm_herding"

COMPONENTS = ["unit tests", "integration tests", "security scan", "licence check", "migration dry-run", "load test"]

SYSTEM = (
    "You are one reviewer on a council of agents deciding whether a software release may advance to the "
    "next stage. You have seen only your own evidence. Decide on your evidence and any information you are "
    "given. Answer with JSON only: {\"choice\": \"advance\" or \"hold\", \"rationale\": \"one or two sentences\"}."
)


def make_proposals(k: int, rng: random.Random) -> List[dict]:
    """Ground truth: the release is good (advance) or has one failing check (hold)."""
    out = []
    for i in range(k):
        good = rng.random() < 0.5
        failing = None if good else rng.choice(COMPONENTS)
        out.append({"id": f"R-{i + 1:03d}", "truth": "advance" if good else "hold", "failing": failing})
    return out


def evidence_for(prop: dict, correct: bool, rng: random.Random) -> str:
    """Private evidence; with probability 1-p it points the wrong way."""
    says_good = (prop["truth"] == "advance") == correct
    comp = rng.choice(COMPONENTS)
    if says_good:
        return f"You re-ran the {comp} for release {prop['id']}: all checks passed and you found no blocking issue."
    bad = prop["failing"] or comp
    return f"You re-ran the {bad} for release {prop['id']}: it failed, and you consider this a blocking issue."


def openai_complete(base: str, model: str, key: str, temperature: float) -> Callable[[str], str]:
    url = base.rstrip("/") + "/chat/completions"

    def complete(prompt: str) -> str:
        body = json.dumps(
            {"model": model, "temperature": temperature, "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]}
        ).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return json.loads(r.read())["choices"][0]["message"]["content"]
            except Exception as e:  # network hiccup: retry the call, not the answer
                if attempt == 2:
                    return f"<error: {e}>"
                time.sleep(2 * (attempt + 1))
        return ""

    return complete


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["mock", "openai"], default="mock")
    ap.add_argument("--proposals", type=int, default=40)
    ap.add_argument("--n", type=int, default=7)
    ap.add_argument("--p", type=float, default=0.7)
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--seed", type=int, default=2026)
    a = ap.parse_args()

    rng = random.Random(a.seed)
    props = make_proposals(a.proposals, rng)
    # evidence draws fixed per (proposal, agent) and reused in both conditions
    draws = {(pr["id"], j): rng.random() < a.p for pr in props for j in range(a.n)}
    ev_rng = random.Random(a.seed + 1)
    evidence = {(pr["id"], j): evidence_for(pr, draws[(pr["id"], j)], ev_rng) for pr in props for j in range(a.n)}

    if a.backend == "openai":
        base = os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1")
        model = os.environ.get("LLM_MODEL")
        if not model:
            sys.exit("set LLM_MODEL (and LLM_BASE_URL / LLM_API_KEY if needed)")
        complete = openai_complete(base, model, os.environ.get("LLM_API_KEY", ""), a.temperature)
        label = model
    else:
        complete, label = None, "mock-conformist"

    run_id = time.strftime("%Y%m%d-%H%M%S") + f"-{a.backend}"
    OUT.mkdir(parents=True, exist_ok=True)
    log = (OUT / f"{run_id}.jsonl").open("w", encoding="utf-8")
    log.write(json.dumps({"meta": {"model": label, "n": a.n, "p": a.p, "temperature": a.temperature, "seed": a.seed, "proposals": a.proposals}}) + "\n")

    organiser = AgentIdentity.generate("organiser")
    stats: Dict[str, Dict[str, float]] = {c: {"correct": 0, "invalid": 0, "against_evidence": 0, "ballots": 0, "audit_fail": 0} for c in ("sealed", "open")}
    paired: Dict[str, Dict[str, int]] = {}
    order_rng = random.Random(a.seed + 2)
    for pr in props:
        order = list(range(a.n))
        order_rng.shuffle(order)
        for cond in ("sealed", "open"):
            agents = []
            for j in order:
                ident = AgentIdentity.generate(f"reviewer-{j + 1}")
                if a.backend == "openai":
                    ag = LLMAgent(ident, complete=complete, system="")
                else:
                    sig = "advance" if (pr["truth"] == "advance") == draws[(pr["id"], j)] else "hold"
                    ag = ConformistAgent(ident, truth=sig, accuracy=1.0, rng=random.Random(0))
                ag._j = j  # evidence index
                agents.append(ag)
            prop = Proposal(pr["id"], f"Advance release {pr['id']} to production?", "Vote advance or hold.", ["advance", "hold"])

            # each agent sees only its own private evidence
            def decide_with_evidence(agent, ctx, prop=prop):
                p2 = Proposal(prop.proposal_id, prop.title, prop.body, prop.choices, {"your evidence": evidence[(pr["id"], agent._j)]})
                return type(agent).decide(agent, p2, ctx)

            for ag in agents:
                ag.decide = (lambda ctx_prop, ctx, ag=ag: decide_with_evidence(ag, ctx))  # type: ignore[assignment]
            run = run_transition_ballot(Event(pr["id"], prop.title), prop, organiser, agents, threshold_k=2, rule="majority", quorum=0.0, sealed=(cond == "sealed"))
            st = stats[cond]
            st["audit_fail"] += 0 if run.audit.ok else 1
            st["correct"] += run.tally.winner == pr["truth"]
            paired.setdefault(pr["id"], {})[cond] = int(run.tally.winner == pr["truth"])
            st["invalid"] += run.tally.invalid
            for ag in agents:
                b = run.election.decrypted.get(ag.identity.agent_id)
                own = "advance" if (pr["truth"] == "advance") == draws[(pr["id"], ag._j)] else "hold"
                st["ballots"] += 1
                if b is not None and b.choice != own:
                    st["against_evidence"] += 1
                log.write(json.dumps({"proposal": pr["id"], "truth": pr["truth"], "condition": cond, "agent": ag.identity.name, "evidence_says": own, "choice": b.choice if b else None, "rationale": b.rationale if b else None}, ensure_ascii=False) + "\n")
            print(f"{pr['id']} {cond:<6} winner={run.tally.winner!s:<8} truth={pr['truth']:<7} counts={run.tally.counts}", flush=True)
    log.close()

    print(f"\nmodel={label}  N={a.n}  p={a.p}  proposals={a.proposals}  log={OUT / (run_id + '.jsonl')}")
    print(f"{'condition':<8}{'P(correct)':>12}{'against own evidence':>23}{'invalid':>9}{'audit fails':>13}")
    for c, st in stats.items():
        print(f"{c:<8}{st['correct'] / a.proposals:>12.3f}{st['against_evidence'] / st['ballots']:>23.3f}{int(st['invalid']):>9}{int(st['audit_fail']):>13}")
    diffs = [v["sealed"] - v["open"] for v in paired.values()]
    brng = random.Random(a.seed + 3)
    boots = sorted(sum(brng.choice(diffs) for _ in diffs) / len(diffs) for _ in range(5000))
    d = sum(diffs) / len(diffs)
    print(f"\nsealed − open accuracy: {d:+.3f}  (paired bootstrap 95% CI {boots[124]:+.3f} to {boots[4874]:+.3f}, {len(diffs)} proposals)")
    print("Same evidence in both conditions. A null result is a result; report it as such.")


if __name__ == "__main__":
    main()
