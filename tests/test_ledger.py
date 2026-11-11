"""Ledger behaviour: persistence, the state hash, ack/park/snooze, and the audit history."""

from __future__ import annotations

from datetime import datetime, timezone


def test_ledger_round_trips_through_disk(hf, tmp_path):
    led = hf.ledger.Ledger.load(tmp_path / "comms")
    led.mark_sent("water-heater", 2, "service due 2026-11-12", "Water heater service due 2026-11-12")
    led.save()

    again = hf.ledger.Ledger.load(tmp_path / "comms")
    rec = again.get("water-heater")
    assert rec is not None
    assert rec.send_count == 1
    assert rec.tier == 2
    assert rec.summary.startswith("Water heater service")


def test_state_hash_ignores_case_and_whitespace(hf):
    a = hf.ledger.state_hash("Service   DUE 2026-11-12")
    b = hf.ledger.state_hash("  service due 2026-11-12 ")
    assert a == b
    assert a != hf.ledger.state_hash("service due 2026-11-13")


def test_ack_resets_the_ladder_and_clears_the_park(hf, tmp_path):
    led = hf.ledger.Ledger.load(tmp_path / "comms")
    led.mark_sent("k", 2, "state", "summary")
    led.mark_sent("k", 2, "state2", "summary2")
    led.park("k", datetime(2026, 12, 1, tzinfo=timezone.utc))
    rec = led.ack("k", datetime(2026, 11, 11, tzinfo=timezone.utc))
    assert rec.send_count == 0
    assert rec.parked_until is None
    assert rec.acked_at == "2026-11-11T00:00:00Z"


def test_history_is_append_only_and_readable(hf, tmp_path):
    led = hf.ledger.Ledger.load(tmp_path / "comms")
    led.append_history({"at": "2026-11-11T09:00:00Z", "agent": "finance", "tier": 2,
                        "key": "k", "sent": True, "code": "allowed", "summary": "s"})
    led.append_history({"at": "2026-11-11T09:05:00Z", "agent": "finance", "tier": 2,
                        "key": "k", "sent": False, "code": "unchanged", "summary": "s"})
    lines = list(hf.ledger.Ledger.load(tmp_path / "comms").read_history())
    assert len(lines) == 2
    assert lines[0]["sent"] is True and lines[1]["code"] == "unchanged"


def test_sends_between_filters_by_tier_and_agent(hf, tmp_path):
    led = hf.ledger.Ledger.load(tmp_path / "comms")
    for agent, tier, sent in (("finance", 2, True), ("home", 2, True), ("finance", 1, True),
                              ("finance", 2, False)):
        led.append_history({"at": "2026-11-11T09:00:00Z", "agent": agent, "tier": tier,
                            "key": "k", "sent": sent, "code": "allowed", "summary": "s"})
    start = datetime(2026, 11, 11, 0, tzinfo=timezone.utc)
    end = datetime(2026, 11, 11, 23, tzinfo=timezone.utc)
    assert len(led.sends_between(start, end, tier=2)) == 2
    assert len(led.sends_between(start, end, tier=2, agent="finance")) == 1
    assert led.last_send_at("home") is not None
    assert led.last_send_at("nobody") is None


def test_digest_queue_is_date_scoped(hf, tmp_path):
    led = hf.ledger.Ledger.load(tmp_path / "comms")
    led.queue_digest_line("finance", "Paid 2 bills ($84)", datetime(2026, 11, 11, 9, tzinfo=timezone.utc))
    led.queue_digest_line("finance", "Old line ($10)", datetime(2026, 11, 1, 9, tzinfo=timezone.utc))
    today = led.digest_lines(datetime(2026, 11, 11, 0, tzinfo=timezone.utc))
    assert len(today) == 1
    assert today[0]["line"].startswith("Paid 2 bills")


def test_active_keys_excludes_parked_and_snoozed(hf, tmp_path):
    led = hf.ledger.Ledger.load(tmp_path / "comms")
    led.mark_sent("live", 2, "s", "s")
    led.mark_sent("parked", 2, "s", "s")
    led.park("parked", datetime(2099, 1, 1, tzinfo=timezone.utc))
    assert led.active_keys() == ["live"]


def test_a_corrupt_ledger_file_does_not_kill_the_gate(hf, tmp_path):
    home = tmp_path / "comms"
    home.mkdir(parents=True)
    (home / "ledger.json").write_text("{not json")
    led = hf.ledger.Ledger.load(home)
    assert led.records == {}
