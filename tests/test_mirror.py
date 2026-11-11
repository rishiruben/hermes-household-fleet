"""The Home Assistant mirror: registry reading, summary building, and the reconcile logic.

The reconcile test replaces the transport (``call_service``) with an in-memory fake, so the
add/update/remove decisions are verified without a network. One optional live test runs only when
``HASS_TOKEN`` is set.
"""

from __future__ import annotations

import json
import os

import pytest


def task(hf, **kw):
    base = dict(id="abc123", agent="finance", title="Pay the water bill", status="open",
                priority="high", due="2026-11-12", note="")
    base.update(kw)
    return hf.ha_mirror.Task(**base)


# ─── registry ────────────────────────────────────────────────────────────────────────────────

def test_load_registry_reads_tasks_and_agents(hf, tmp_path):
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps({
        "agents": ["finance", "home"],
        "tasks": [
            {"id": "1", "agent": "finance", "title": "Pay the water bill", "status": "open",
             "priority": "high", "due": "2026-11-12"},
            {"id": "2", "agent": "home", "title": "Book the boiler service", "status": "done"},
        ],
    }))
    tasks, agents = hf.ha_mirror.load_registry(path)
    assert agents == ["finance", "home"]
    assert tasks[0].title == "Pay the water bill"
    assert tasks[1].status == "done"


def test_missing_registry_raises_file_not_found(hf, tmp_path):
    with pytest.raises(FileNotFoundError):
        hf.ha_mirror.load_registry(tmp_path / "nope.json")


# ─── summary ─────────────────────────────────────────────────────────────────────────────────

def test_summary_lists_active_tasks_high_priority_first(hf):
    tasks = [
        task(hf, id="1", title="Low thing", priority="low"),
        task(hf, id="2", title="High thing", priority="high"),
        task(hf, id="3", title="Finished thing", status="done"),
    ]
    summary = hf.ha_mirror.build_summary("finance", tasks)
    assert summary.splitlines()[0] == "finance: 2 active"
    assert "High thing" in summary.splitlines()[1]
    assert "Finished thing" not in summary


def test_summary_stays_within_the_input_text_limit(hf):
    tasks = [task(hf, id=str(i), title=f"Task {i} " + "x" * 40) for i in range(40)]
    assert len(hf.ha_mirror.build_summary("finance", tasks)) <= hf.ha_mirror.INPUT_TEXT_MAX


def test_summary_says_zero_for_a_clean_agent(hf):
    assert hf.ha_mirror.build_summary("finance", []).startswith("finance: 0 active")


# ─── todo reconcile ──────────────────────────────────────────────────────────────────────────

class FakeHA:
    """In-memory stand-in for Home Assistant's todo service calls."""

    def __init__(self, items=None, entity_exists=True):
        self.entity_exists = entity_exists
        self.items = {i["summary"]: dict(i) for i in (items or [])}
        self.calls = []

    def __call__(self, config, domain, service, payload, *, return_response=False):
        self.calls.append((domain, service, dict(payload)))
        if domain == "todo" and service == "get_items":
            if not self.entity_exists:
                return {"message": "Entity not found"}
            return {"service_response": {"todo.tasks_finance": {"items": list(self.items.values())}}}
        if domain == "todo" and service == "add_item":
            self.items[payload["item"]] = {"summary": payload["item"], "status": "needs_action",
                                           "due": payload.get("due_date")}
            return {}
        if domain == "todo" and service == "update_item":
            self.items[payload["item"]]["status"] = payload.get("status", "needs_action")
            return {}
        if domain == "todo" and service == "remove_item":
            self.items.pop(payload["item"], None)
            return {}
        return {}


def test_reconcile_adds_updates_and_removes(hf, monkeypatch):
    fake = FakeHA(items=[
        {"summary": "Pay the water bill", "status": "completed", "due": None},   # needs re-opening
        {"summary": "Stale task", "status": "needs_action", "due": None},        # must be removed
    ])
    monkeypatch.setattr(hf.ha_mirror, "call_service", fake)
    tasks = [task(hf, title="Pay the water bill", status="open"),
             task(hf, id="9", title="Book the boiler service", status="open")]
    report = hf.ha_mirror.sync_todo_list(hf.ha_mirror.MirrorConfig(token="x"), "finance", tasks)
    assert report == "1 added, 1 updated, 1 removed"
    assert fake.items["Pay the water bill"]["status"] == "needs_action"
    assert "Book the boiler service" in fake.items
    assert "Stale task" not in fake.items


def test_reconcile_is_idempotent(hf, monkeypatch):
    fake = FakeHA(items=[{"summary": "Pay the water bill", "status": "needs_action", "due": "2026-11-12"}])
    monkeypatch.setattr(hf.ha_mirror, "call_service", fake)
    tasks = [task(hf, title="Pay the water bill", status="open")]
    report = hf.ha_mirror.sync_todo_list(hf.ha_mirror.MirrorConfig(token="x"), "finance", tasks)
    assert report == "0 added, 0 updated, 0 removed"


def test_missing_todo_entity_is_skipped_not_raised(hf, monkeypatch):
    fake = FakeHA(entity_exists=False)
    monkeypatch.setattr(hf.ha_mirror, "call_service", fake)
    report = hf.ha_mirror.sync_todo_list(hf.ha_mirror.MirrorConfig(token="x"), "finance",
                                         [task(hf)])
    assert report == "skipped (no todo entity)"


def test_mirror_reports_every_agent_and_never_raises_per_agent(hf, monkeypatch, tmp_path):
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps({
        "agents": ["finance", "home"],
        "tasks": [{"id": "1", "agent": "finance", "title": "Pay the water bill", "status": "open"},
                  {"id": "2", "agent": "home", "title": "Book the boiler service", "status": "open"}],
    }))
    sent = []

    def fake_call(config, domain, service, payload, *, return_response=False):
        sent.append((domain, service, payload))
        if domain == "todo":
            return {"message": "not found"}
        return {}

    monkeypatch.setattr(hf.ha_mirror, "call_service", fake_call)
    result = hf.ha_mirror.mirror(path, config=hf.ha_mirror.MirrorConfig(token="x"))
    assert result["ok"] is True
    assert [r["agent"] for r in result["results"]] == ["finance", "home"]
    assert all(r["input_text"] == "ok" for r in result["results"])
    assert any(p["entity_id"] == "input_text.priority_home" for _d, _s, p in sent)


def test_missing_token_is_an_explicit_error(hf):
    with pytest.raises(hf.ha_mirror.HomeAssistantError):
        hf.ha_mirror.call_service(hf.ha_mirror.MirrorConfig(token=""), "input_text", "set_value", {})


# ─── optional live check ─────────────────────────────────────────────────────────────────────

@pytest.mark.skipif(not os.environ.get("HASS_TOKEN"), reason="HASS_TOKEN not set")
def test_live_input_text_write(hf):
    config = hf.ha_mirror.MirrorConfig()
    hf.ha_mirror.set_input_text(config, "input_text.priority_smoke_test", "household-fleet smoke test")
