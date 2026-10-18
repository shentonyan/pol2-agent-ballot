"""Regression cases for forged results and malformed exported evidence."""
import copy

import pytest

from pol2_agent_ballot.ballot import Ballot, Envelope, canonical, sha256
from pol2_agent_ballot.election import Election, ElectionConfig, RejectedBallot
from pol2_agent_ballot.identity import AgentIdentity, build_registry
from pol2_agent_ballot.tally import tally
from pol2_agent_ballot.verify import Auditor


def election_with_votes(choices, *, n=4, quorum=0.5, rule="majority"):
    org = AgentIdentity.generate("org")
    agents = [AgentIdentity.generate(f"a{i}") for i in range(n)]
    config = ElectionConfig("audit-test", "advance?", ["advance", "hold"],
                            quorum=quorum, rule=rule)
    election = Election(config, build_registry(org, agents), org)
    shares = election.generate_keys()
    election.open()
    for agent, choice in zip(agents, choices):
        election.accept(Envelope.cast(agent, Ballot(config.election_id, agent.agent_id,
                                                    choice, "evidence"), election.public_key))
    election.close()
    election.decrypt(shares[:2])
    tally(election)
    return election


def rehash(log):
    previous = "0" * 64
    for entry in log:
        entry["prev_hash"] = previous
        entry["hash"] = sha256(canonical({k: entry[k] for k in
                                          ("seq", "kind", "payload", "prev_hash", "ts")}))
        previous = entry["hash"]
    return log


@pytest.mark.parametrize("choices,quorum,rule", [
    (["advance", "hold"], 0.5, "majority"),  # tie
    (["advance"], 1.0, "majority"),          # no quorum
    ([], 0.0, "majority"),                   # no valid ballots
    (["advance", "advance", "hold"], 0.5, "unanimous"),  # threshold missed
])
def test_auditor_rejects_forged_approval_without_winning_vote(choices, quorum, rule):
    election = election_with_votes(choices, quorum=quorum, rule=rule)
    assert Auditor(election.export_log(), election.export_envelopes()).run().ok
    log = copy.deepcopy(election.export_log())
    result = next(e["payload"] for e in log if e["kind"] == "tally")
    result.update(decided=True, winner="advance")
    report = Auditor(rehash(log), election.export_envelopes()).run()
    assert not report.ok
    assert any("decided" in f for f in report.failures)


@pytest.mark.parametrize("field,value", [
    ("winner", "advance"), ("eligible", 999), ("valid", 999),
    ("invalid", 999), ("rule", "plurality"), ("rationales", {}),
])
def test_auditor_checks_complete_tally(field, value):
    election = election_with_votes(["advance", "advance", "hold"], rule="unanimous")
    log = copy.deepcopy(election.export_log())
    next(e["payload"] for e in log if e["kind"] == "tally")[field] = value
    assert not Auditor(rehash(log), election.export_envelopes()).run().ok


def test_duplicate_export_cannot_replace_missing_revote():
    org = AgentIdentity.generate("org")
    agent = AgentIdentity.generate("voter")
    election = Election(ElectionConfig("E", "?", ["advance", "hold"]),
                        build_registry(org, [agent]), org)
    shares = election.generate_keys()
    election.open()
    for reason in ("first", "revised"):
        election.accept(Envelope.cast(agent, Ballot("E", agent.agent_id, "advance", reason),
                                      election.public_key))
    election.close()
    election.decrypt(shares[:2])
    tally(election)
    envelopes = election.export_envelopes()
    assert Auditor(election.export_log(), envelopes).run().ok
    # Same export length and same final vote; the older envelope is missing.
    assert not Auditor(election.export_log(), [envelopes[1], envelopes[1]]).run().ok


@pytest.mark.parametrize("change", ["missing_config", "duplicate_tally", "bad_sequence",
                                    "ballot_after_close", "truncated_decrypt"])
def test_auditor_rejects_invalid_log_structure(change):
    election = election_with_votes(["advance", "advance"])
    log = copy.deepcopy(election.export_log())
    if change == "missing_config":
        log.pop(0)
    elif change == "duplicate_tally":
        log.append(copy.deepcopy(log[-1]))
    elif change == "bad_sequence":
        log[-1]["seq"] = 900
    elif change == "ballot_after_close":
        ballot = next(e for e in log if e["kind"] == "ballot")
        close = next(e for e in log if e["kind"] == "close")
        ballot["kind"], close["kind"] = close["kind"], ballot["kind"]
    else:
        log[:] = [e for e in log if e["kind"] != "decrypt"]
    assert not Auditor(rehash(log), election.export_envelopes()).run().ok


@pytest.mark.parametrize("log,envelopes", [([], []), ([{}], []), (None, []),
                                          ([{"kind": "config"}], []), ([], [{}])])
def test_malformed_exports_return_failure_instead_of_raising(log, envelopes):
    report = Auditor(log, envelopes).run()
    assert not report.ok and report.failures


def test_lifecycle_is_one_way_and_duplicate_submission_is_rejected():
    org = AgentIdentity.generate("org")
    agent = AgentIdentity.generate("voter")
    election = Election(ElectionConfig("E", "?", ["advance", "hold"]),
                        build_registry(org, [agent]), org)
    shares = election.generate_keys()
    with pytest.raises(RuntimeError):
        election.generate_keys()
    election.open()
    env = Envelope.cast(agent, Ballot("E", agent.agent_id, "advance"), election.public_key)
    election.accept(env)
    with pytest.raises(RejectedBallot):
        election.accept(env)
    with pytest.raises(RuntimeError):
        election.open()
    election.close()
    with pytest.raises(RuntimeError):
        election.open()
    election.decrypt(shares[:2])
    with pytest.raises(RuntimeError):
        election.open()


@pytest.mark.parametrize("kwargs", [
    {"choices": []}, {"choices": ["advance", "advance"]}, {"choices": ["advance", 3]},
    {"rule": "unknown"}, {"quorum": -1}, {"quorum": 1.1}, {"quorum": float("nan")},
    {"threshold_k": 0}, {"threshold_n": 1}, {"threshold_k": 1.5},
    {"verification_window_s": -1}, {"verification_window_s": float("inf")},
])
def test_invalid_election_configuration_rejected(kwargs):
    config = {"election_id": "E", "question": "?", "choices": ["advance", "hold"]}
    config.update(kwargs)
    with pytest.raises(ValueError):
        ElectionConfig(**config)


def test_sealed_context_cannot_be_overridden_by_extra_context():
    from pol2_agent_ballot.agents import Proposal
    from pol2_agent_ballot.event import Event, run_transition_ballot

    class ObservingAgent:
        def __init__(self, name):
            self.identity = AgentIdentity.generate(name)

        def decide(self, proposal, context):
            assert context["visible_votes"] == []
            assert context["stage"] == "proposed"
            assert context["round"] == 2
            return "advance", "independent evidence"

    agents = [ObservingAgent("a"), ObservingAgent("b")]
    run = run_transition_ballot(
        Event("context-test", "context"), Proposal("P", "?", "", ["advance", "hold"]),
        AgentIdentity.generate("organiser"), agents,
        extra_context={"visible_votes": [("peer", "hold")], "stage": "executed", "round": 2},
    )
    assert run.audit.ok


@pytest.mark.parametrize("change", ["missing", "signature", "position", "reorder"])
def test_receipts_bind_envelopes_to_original_ledger_positions(change):
    election = election_with_votes(["advance", "advance", "hold"])
    log = copy.deepcopy(election.export_log())
    envelopes = copy.deepcopy(election.export_envelopes())
    if change == "missing":
        envelopes[0]["receipt"] = None
    elif change == "signature":
        envelopes[0]["receipt"]["collector_signature"] = "00" * 64
    elif change == "position":
        envelopes[0]["receipt"]["log_seq"] = 999
    else:
        indices = [i for i, e in enumerate(log) if e["kind"] == "ballot"]
        a, b = indices[:2]
        log[a]["payload"], log[b]["payload"] = log[b]["payload"], log[a]["payload"]
        rehash(log)
    assert not Auditor(log, envelopes).run().ok
