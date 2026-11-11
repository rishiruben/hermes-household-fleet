"""The gate's nine checks, one test per check plus the interesting combinations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

ET = ZoneInfo("America/New_York")


def at(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=ET).astimezone(timezone.utc)


def req(hf, **kw):
    base = dict(agent="finance", key="water-heater", text="Water heater service is due 2026-11-12 — book it?",
                tier=2, state="service due 2026-11-12")
    base.update(kw)
    return hf.gate.MessageRequest(**base)


# ─── the allow path, and the anti-repeat that makes the gate worth having ────────────────────

def test_first_message_is_allowed(hf, ledger, config):
    decision = hf.gate.decide(req(hf), ledger, config, now=at(2026, 11, 11, 9))
    assert decision.allowed and decision.code == "allowed"


def test_same_key_same_state_is_never_sent_twice(hf, ledger, config):
    first = hf.gate.decide(req(hf), ledger, config, now=at(2026, 11, 11, 9))
    hf.gate.record_decision(first, req(hf), ledger, now=at(2026, 11, 11, 9))
    again = hf.gate.decide(req(hf), ledger, config, now=at(2026, 11, 11, 20))
    assert not again.allowed and again.code == "unchanged"


def test_reworded_message_with_unchanged_facts_is_still_a_repeat(hf, ledger, config):
    """The hash covers the facts, not the prose — rewording must not slip past the gate."""
    first = req(hf)
    hf.gate.record_decision(hf.gate.decide(first, ledger, config, now=at(2026, 11, 11, 9)), first, ledger,
                            now=at(2026, 11, 11, 9))
    reworded = req(
        hf,
        text="Quick reminder about the water heater on 2026-11-12 — shall I book it?",
        state="  Service   DUE 2026-11-12 ",
    )
    decision = hf.gate.decide(reworded, ledger, config, now=at(2026, 11, 11, 20))
    assert not decision.allowed and decision.code == "unchanged"


def test_changed_state_is_deliverable_after_an_ack(hf, ledger, config):
    first = req(hf)
    hf.gate.record_decision(hf.gate.decide(first, ledger, config, now=at(2026, 11, 11, 9)), first, ledger,
                            now=at(2026, 11, 11, 9))
    ledger.ack("water-heater", at(2026, 11, 11, 10))
    changed = req(hf, state="service booked 2026-11-13", text="Water heater booked for 2026-11-13.")
    decision = hf.gate.decide(changed, ledger, config, now=at(2026, 11, 11, 11))
    assert decision.allowed


# ─── quiet hours ─────────────────────────────────────────────────────────────────────────────

def test_quiet_hours_are_absolute_for_ordinary_messages(hf, ledger, config):
    decision = hf.gate.decide(req(hf), ledger, config, now=at(2026, 11, 11, 23, 30))
    assert not decision.allowed and decision.code == "quiet_hours"


def test_quiet_hours_wrap_midnight(hf, ledger, config):
    assert hf.gate.in_quiet_hours(at(2026, 11, 12, 3), config)
    assert not hf.gate.in_quiet_hours(at(2026, 11, 12, 12), config)


def test_safety_tier_breaks_quiet_hours(hf, ledger, config):
    leak = req(hf, agent="home", key="leak-sink", tier=1, safety=True,
               text="Water pooling under the kitchen sink — shut the supply valve?",
               state="leak active")
    decision = hf.gate.decide(leak, ledger, config, now=at(2026, 11, 12, 2))
    assert decision.allowed


def test_tier_one_without_safety_flag_still_respects_quiet_hours(hf, ledger, config):
    leak = req(hf, agent="home", key="leak-sink", tier=1, safety=False, text="Leak? valve?")
    decision = hf.gate.decide(leak, ledger, config, now=at(2026, 11, 12, 2))
    assert not decision.allowed and decision.code == "quiet_hours"


# ─── park / snooze ───────────────────────────────────────────────────────────────────────────

def test_parked_key_is_silent(hf, ledger, config):
    ledger.park("water-heater", at(2026, 11, 20, 12))
    decision = hf.gate.decide(req(hf), ledger, config, now=at(2026, 11, 11, 9))
    assert not decision.allowed and decision.code == "parked"


def test_snooze_resets_the_ladder(hf, ledger, config):
    first = req(hf)
    hf.gate.record_decision(hf.gate.decide(first, ledger, config, now=at(2026, 11, 11, 9)), first, ledger,
                            now=at(2026, 11, 11, 9))
    ledger.snooze("water-heater", at(2026, 11, 15, 12))
    assert ledger.get("water-heater").send_count == 0
    before = hf.gate.decide(req(hf, state="changed facts 1"), ledger, config, now=at(2026, 11, 11, 12))
    assert not before.allowed and before.code == "snoozed"


# ─── ladder, caps, coalescing, value test ────────────────────────────────────────────────────

def test_ladder_holds_the_second_nudge_for_48_hours(hf, ledger, config):
    first = req(hf)
    hf.gate.record_decision(hf.gate.decide(first, ledger, config, now=at(2026, 11, 9, 9)), first, ledger,
                            now=at(2026, 11, 9, 9))
    too_soon = req(hf, state="still due 2026-11-12", text="Still due 2026-11-12?")
    decision = hf.gate.decide(too_soon, ledger, config, now=at(2026, 11, 9, 20))
    assert not decision.allowed and decision.code == "ladder"
    due = hf.gate.decide(too_soon, ledger, config, now=at(2026, 11, 11, 10))
    assert due.allowed


def test_three_unanswered_nudges_park_the_key(hf, ledger, config):
    # Nudge 1, then 48 h later nudge 2, then 168 h after that nudge 3 — the ladder's whole shape.
    for hour, state in ((0, "due 2026-11-12"), (50, "due 2026-11-12 still"), (250, "due, unbooked")):
        moment = at(2026, 11, 1, 9) + timedelta(hours=hour)
        r = req(hf, state=state, text=f"{state}?")
        decision = hf.gate.decide(r, ledger, config, now=moment)
        assert decision.allowed, decision.code
        hf.gate.record_decision(decision, r, ledger, now=moment)
    assert ledger.get("water-heater").send_count == 3
    blocked = req(hf, state="fourth try", text="fourth try 2026-11-12?")
    moment = at(2026, 11, 20, 9)
    decision = hf.gate.decide(blocked, ledger, config, now=moment)
    assert not decision.allowed and decision.code == "ladder"
    # `decide()` is pure; applying the verdict is what parks the key.
    hf.gate.record_decision(decision, blocked, ledger, now=moment)
    assert ledger.get("water-heater").parked_until is not None


def test_per_agent_daily_cap(hf, ledger, config):
    for i in range(config.t2_per_agent_per_day):
        # 40 minutes apart: inside the same day, outside the coalescing window.
        moment = at(2026, 11, 11, 9) + timedelta(minutes=40 * i)
        r = req(hf, key=f"subject-{i}", text=f"Item {i} needs a decision by 2026-11-12?", state=f"item {i}")
        assert hf.gate.decide(r, ledger, config, now=moment).allowed
        hf.gate.record_decision(hf.gate.decide(r, ledger, config, now=moment), r, ledger, now=moment)
    over = req(hf, key="subject-x", text="Item X needs a decision by 2026-11-12?", state="item x")
    decision = hf.gate.decide(over, ledger, config, now=at(2026, 11, 11, 14))
    assert not decision.allowed and decision.code == "cap_agent"


def test_fleet_daily_cap_across_agents(hf, ledger, config):
    agents = ["finance", "health", "home", "writing"]
    for i, agent in enumerate(agents[: config.t2_per_fleet_per_day]):
        r = req(hf, agent=agent, key=f"{agent}-1", text=f"{agent} item due 2026-11-12?", state="s")
        hf.gate.record_decision(hf.gate.decide(r, ledger, config, now=at(2026, 11, 11, 8, i)), r, ledger,
                                now=at(2026, 11, 11, 8, i))
    over = req(hf, agent="fitness", key="fitness-1", text="Fitness item due 2026-11-12?", state="s")
    decision = hf.gate.decide(over, ledger, config, now=at(2026, 11, 11, 14))
    assert not decision.allowed and decision.code in {"cap_fleet", "cap_agent"}


def test_coalescing_window_keeps_one_agent_to_one_message(hf, ledger, config):
    first = req(hf)
    hf.gate.record_decision(hf.gate.decide(first, ledger, config, now=at(2026, 11, 11, 9)), first, ledger,
                            now=at(2026, 11, 11, 9))
    second = req(hf, key="other", text="Other thing due 2026-11-12?", state="other")
    decision = hf.gate.decide(second, ledger, config, now=at(2026, 11, 11, 9, 10))
    assert not decision.allowed and decision.code == "coalesced"


def test_value_free_status_report_is_rejected(hf, ledger, config):
    decision = hf.gate.decide(req(hf, key="loan", text="Just checking in on the loan documents.",
                                  state="no change"), ledger, config, now=at(2026, 11, 11, 9))
    assert not decision.allowed and decision.code == "value_free"


def test_a_number_saves_a_message_from_the_value_test(hf, ledger, config):
    decision = hf.gate.decide(req(hf, key="loan", text="Still waiting on 2 loan documents.",
                                  state="2 outstanding"), ledger, config, now=at(2026, 11, 11, 9))
    assert decision.allowed


def test_a_question_saves_a_message_from_the_value_test(hf, ledger, config):
    decision = hf.gate.decide(req(hf, key="loan", text="Still waiting on the loan documents — call them?",
                                  state="no change"), ledger, config, now=at(2026, 11, 11, 9))
    assert decision.allowed


def test_rollout_switch_off_refuses_everything(hf, ledger, config):
    dead = hf.config.GateConfig(enabled=False)
    decision = hf.gate.decide(req(hf), ledger, dead, now=at(2026, 11, 11, 9))
    assert not decision.allowed and decision.code == "disabled"


# ─── digest lines ────────────────────────────────────────────────────────────────────────────

def test_digest_lines_are_queued_and_capped(hf, ledger, config):
    for i in range(config.t3_lines_per_agent_per_day):
        decision = hf.gate.decide_digest_line("finance", f"Paid bill {i} ($40)", ledger, config,
                                              now=at(2026, 11, 11, 9, i))
        assert decision.allowed
    over = hf.gate.decide_digest_line("finance", "Paid another bill ($50)", ledger, config,
                                     now=at(2026, 11, 11, 10))
    assert not over.allowed and over.code == "cap_agent"
    assert len(ledger.digest_lines(hf.gate.day_start(at(2026, 11, 11, 12), config))) == 2


def test_digest_never_carries_a_value_free_line(hf, ledger, config):
    decision = hf.gate.decide_digest_line("finance", "no update", ledger, config, now=at(2026, 11, 11, 9))
    assert not decision.allowed and decision.code == "value_free"


# ─── the ledger's audit trail ────────────────────────────────────────────────────────────────

def test_every_decision_is_auditable_including_refusals(hf, ledger, config):
    r = req(hf)
    hf.gate.record_decision(hf.gate.decide(r, ledger, config, now=at(2026, 11, 11, 9)), r, ledger,
                            now=at(2026, 11, 11, 9))
    repeat = req(hf)
    hf.gate.record_decision(hf.gate.decide(repeat, ledger, config, now=at(2026, 11, 11, 20)), repeat,
                            ledger, now=at(2026, 11, 11, 20))
    history = ledger.read_history()
    assert [h["sent"] for h in history] == [True, False]
    assert history[1]["code"] == "unchanged"
    assert history[1]["agent"] == "finance"
