# Pre-registered protocol — do LLM agents herd under open voting?

**Status: designed, not run.** Nothing in this repo claims a result for LLM
agents. `sealed_vs_open.py` only demonstrates the mechanism with scripted
conformists.

## Question
When several LLM agents each receive a private, noisy piece of evidence and
must vote on whether an event advances, does showing them the running tally
(open sequential voting) lower the accuracy of the collective decision
compared with sealed voting?

## Design
* N = 7 agents, same model, same system prompt (`agents.LLMAgent.system`).
* 40 proposals with a ground-truth answer (e.g. "does the attached test log
  show all tests passing?") built so that each agent's private evidence is
  correct with p ≈ 0.7 — evidence is corrupted independently per agent.
* Two conditions, same proposals, same evidence draws:
  **sealed** (`sealed=True`) and **open** (`sealed=False`, agents vote in a
  random order and see prior votes).
* Primary outcome: fraction of proposals where the majority decision is
  correct. Secondary: fraction of agents whose vote contradicts their own
  private evidence, split by whether a visible majority existed.
* Analysis: paired comparison per proposal; report the difference with a
  bootstrap CI. A null result is reported as a result.

## Honesty clauses
* Temperature, model version, prompt and seed are recorded in the output.
* The scripted simulation is not evidence about LLMs; only this run is.
* Results apply to this task family and model; no claim about humans.
* If the model refuses or returns unparseable output, the ballot is invalid
  (not re-prompted) and the rate is reported.

## How to run (once a completion function is wired)
```python
from pol2_agent_ballot.agents import LLMAgent
agent = LLMAgent(identity, complete=my_completion_fn)
```
Then adapt `sealed_vs_open.py` to build `LLMAgent`s instead of
`ConformistAgent`s. No API keys or endpoints belong in this repository.
