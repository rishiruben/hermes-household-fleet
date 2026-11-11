"""Tool registration for the household-fleet plugin.

Five tools, all thin wrappers over ``gate.py`` / ``ledger.py`` / ``ha_mirror.py`` so the policy
and the mirror stay testable without Hermes:

* ``fleet_comms_decide`` — run the gate and return the verdict *without* touching the ledger.
* ``fleet_comms_send``   — decide, record, and hand back the text to deliver (or the reason it
  must not be delivered). Delivery itself stays in the host agent's own channel.
* ``fleet_comms_state``  — ack / park / snooze one subject key.
* ``fleet_comms_status`` — what has been said today, what is parked, what the ladder holds.
* ``fleet_tasks_push``   — mirror the task registry to Home Assistant.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import gate as gate_mod
from .config import GateConfig
from .ha_mirror import mirror
from .ledger import Ledger, iso, now_utc, parse_iso

TOOLSET = "household_fleet"


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _ok(**payload: Any) -> str:
    return _json({"ok": True, **payload})


def _err(message: str, **payload: Any) -> str:
    return _json({"ok": False, "error": message, **payload})


def _tier_or_error(tier: Any) -> Optional[int]:
    try:
        value = int(tier)
    except (TypeError, ValueError):
        return None
    return value if value in (1, 2, 3) else None


# ─── handlers ────────────────────────────────────────────────────────────────────────────────

def fleet_comms_decide(args: Dict[str, Any], **_: Any) -> str:
    tier = _tier_or_error(args.get("tier", 2))
    if tier is None:
        return _err("tier must be 1, 2 or 3")
    agent = str(args.get("agent") or "").strip()
    key = str(args.get("key") or "").strip()
    text = str(args.get("text") or "")
    if not agent or not key:
        return _err("agent and key are required")
    ledger = Ledger.load()
    config = GateConfig.load()
    request = gate_mod.MessageRequest(
        agent=agent, key=key, text=text, tier=tier,
        state=str(args.get("state") or ""), safety=bool(args.get("safety")),
    )
    decision = gate_mod.decide(request, ledger, config)
    return _ok(decision=decision.to_dict(), state_directory=str(ledger.home))


def fleet_comms_send(args: Dict[str, Any], **_: Any) -> str:
    tier = _tier_or_error(args.get("tier", 2))
    if tier is None:
        return _err("tier must be 1, 2 or 3")
    agent = str(args.get("agent") or "").strip()
    key = str(args.get("key") or "").strip()
    text = str(args.get("text") or "")
    if not agent or not key or not text:
        return _err("agent, key and text are required")

    ledger = Ledger.load()
    config = GateConfig.load()

    if tier == gate_mod.TIER_DIGEST:
        decision = gate_mod.decide_digest_line(agent, text, ledger, config)
        return _ok(
            decision=decision.to_dict(),
            deliver=False,
            guidance="Digest lines are never delivered on their own; they are flushed once daily.",
        )

    request = gate_mod.MessageRequest(
        agent=agent, key=key, text=text, tier=tier,
        state=str(args.get("state") or ""), safety=bool(args.get("safety")),
    )
    decision = gate_mod.decide(request, ledger, config)
    entry = gate_mod.record_decision(decision, request, ledger)
    if decision.allowed:
        return _ok(
            decision=decision.to_dict(),
            deliver=True,
            text=text,
            logged=entry,
            guidance="The gate allows this message. Deliver it on your own channel now; "
                     "it is already recorded, so re-sending the same state will be refused.",
        )
    return _ok(
        decision=decision.to_dict(),
        deliver=False,
        logged=entry,
        guidance="The gate refused this message. Say nothing; the refusal is logged and auditable.",
    )


def fleet_comms_state(args: Dict[str, Any], **_: Any) -> str:
    agent = str(args.get("agent") or "").strip()
    key = str(args.get("key") or "").strip()
    action = str(args.get("action") or "").strip().lower()
    if not key or action not in {"ack", "park", "snooze"}:
        return _err("key is required and action must be ack, park or snooze")
    ledger = Ledger.load()
    when = now_utc()
    if action == "ack":
        rec = ledger.ack(key, when)
    else:
        raw = str(args.get("until") or "").strip()
        parsed: Optional[datetime] = parse_iso(raw)
        if parsed is None and raw:
            try:
                parsed = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                return _err("until must be YYYY-MM-DD or an ISO-8601 UTC timestamp")
        default_days = 7 if action == "park" else 1
        parsed = parsed or when + timedelta(days=default_days)
        rec = ledger.park(key, parsed) if action == "park" else ledger.snooze(key, parsed)
    ledger.save()
    ledger.append_history({"at": iso(when), "agent": agent, "key": key, "sent": False,
                           "code": f"state:{action}", "summary": f"{action} -> {rec.to_dict()}"})
    return _ok(key=key, action=action, record=rec.to_dict())


def fleet_comms_status(args: Dict[str, Any], **_: Any) -> str:
    ledger = Ledger.load()
    config = GateConfig.load()
    now = now_utc()
    start = gate_mod.day_start(now, config)
    agent = str(args.get("agent") or "").strip() or None
    sent_today = ledger.sends_between(start, now, agent=agent)
    payload: Dict[str, Any] = {
        "state_directory": str(ledger.home),
        "gate_enabled": config.enabled,
        "quiet_hours": f"{config.quiet_hours_start}-{config.quiet_hours_end} {config.timezone}",
        "in_quiet_hours": gate_mod.in_quiet_hours(now, config),
        "sent_today": len(sent_today),
        "sent_today_by_tier": {
            str(t): len([e for e in sent_today if e.get("tier") == t]) for t in (1, 2, 3)
        },
        "caps": {
            "t2_per_agent_per_day": config.t2_per_agent_per_day,
            "t2_per_fleet_per_day": config.t2_per_fleet_per_day,
        },
        "digest_lines_today": len(ledger.digest_lines(start, agent=agent)),
    }
    parked: List[Dict[str, Any]] = []
    for key, rec in ledger.records.items():
        until = parse_iso(rec.parked_until) or parse_iso(rec.snooze_until)
        if until and until > now:
            parked.append({"key": key, "until": iso(until), "send_count": rec.send_count})
    payload["parked"] = sorted(parked, key=lambda p: p["until"])
    payload["keys_tracked"] = len(ledger.records)
    if agent:
        payload["agent"] = agent
        payload["last_send_at"] = iso(ledger.last_send_at(agent)) if ledger.last_send_at(agent) else None
    return _ok(**payload)


def fleet_tasks_push(args: Dict[str, Any], **_: Any) -> str:
    raw_path = str(args.get("registry_path") or "").strip()
    path = Path(raw_path).expanduser() if raw_path else None
    agent = str(args.get("agent") or "").strip() or None
    sync_todo = not bool(args.get("no_todo", False))
    try:
        result = mirror(path, agent=agent, sync_todo=sync_todo)
    except FileNotFoundError as exc:
        return _err(str(exc))
    except Exception as exc:  # noqa: BLE001 - surfaced to the agent, never raised into the loop
        return _err(f"{type(exc).__name__}: {exc}")
    return _ok(**result)


# ─── schemas ─────────────────────────────────────────────────────────────────────────────────

_AGENT = {"type": "string", "description": "Agent/profile name the message belongs to (e.g. 'finance')."}
_KEY = {"type": "string",
        "description": "Stable identity of the SUBJECT, not the message — 'task:abc123' or a slug "
                       "like 'water-heater-service'. Same subject across agents uses the same key."}
_STATE = {"type": "string",
          "description": "The salient facts the message is about ('service booked 2026-11-12'). Two "
                         "messages with the same facts can never both be sent."}
_TIER = {"type": "integer", "enum": [1, 2, 3],
         "description": "1 = interrupt/safety (may break quiet hours with safety: true), "
                        "2 = needs a decision today (budgeted), 3 = digest line (never a DM)."}

SCHEMAS: Dict[str, Dict[str, Any]] = {
    "fleet_comms_decide": {
        "name": "fleet_comms_decide",
        "description": "Ask the message gate whether this message may be sent, WITHOUT recording "
                       "anything. Returns allowed/reason plus the code of the first failed check "
                       "(disabled, quiet_hours, parked, snoozed, unchanged, acked_no_change, ladder, "
                       "cap_agent, cap_fleet, coalesced, value_free).",
        "parameters": {
            "type": "object",
            "properties": {"agent": _AGENT, "key": _KEY, "state": _STATE, "tier": _TIER,
                           "text": {"type": "string", "description": "The message body you intend to send."},
                           "safety": {"type": "boolean", "description": "Tier-1 only: genuine safety "
                                                                      "alert that may break quiet hours."}},
            "required": ["agent", "key", "text"],
        },
    },
    "fleet_comms_send": {
        "name": "fleet_comms_send",
        "description": "The only sanctioned send path. Runs the gate, records the decision (allowed "
                       "or refused) in the auditable ledger, and returns whether you should deliver "
                       "the text on your own channel. A refusal is a successful outcome: say nothing. "
                       "tier 3 queues a digest line instead of sending.",
        "parameters": {
            "type": "object",
            "properties": {"agent": _AGENT, "key": _KEY, "state": _STATE, "tier": _TIER,
                           "text": {"type": "string", "description": "The message body to send."},
                           "safety": {"type": "boolean", "description": "Tier-1 only: genuine safety alert."}},
            "required": ["agent", "key", "text"],
        },
    },
    "fleet_comms_state": {
        "name": "fleet_comms_state",
        "description": "Update one subject key: 'ack' (the person replied or the item was touched — "
                       "silences it and resets the nudge ladder), 'park' (stop nudging; the board "
                       "reminds instead), or 'snooze' (remind me later).",
        "parameters": {
            "type": "object",
            "properties": {
                "agent": _AGENT, "key": _KEY,
                "action": {"type": "string", "enum": ["ack", "park", "snooze"]},
                "until": {"type": "string", "description": "park/snooze only: YYYY-MM-DD or an "
                                                           "ISO-8601 UTC timestamp."},
            },
            "required": ["key", "action"],
        },
    },
    "fleet_comms_status": {
        "name": "fleet_comms_status",
        "description": "Read the gate's own state: messages sent today by tier, digest lines queued, "
                       "each cap, whether quiet hours are live, and every parked/snoozed key. Read "
                       "this FIRST on any scheduled wake — most wakes should end in silence.",
        "parameters": {
            "type": "object",
            "properties": {"agent": {"type": "string", "description": "Optional: restrict to one agent."}},
            "required": [],
        },
    },
    "fleet_tasks_push": {
        "name": "fleet_tasks_push",
        "description": "Mirror a task registry JSON to Home Assistant: a compact summary into "
                       "input_text.priority_<agent> and a reconciled native to-do list at "
                       "todo.tasks_<agent>. Idempotent; a missing to-do entity is reported, not an error.",
        "parameters": {
            "type": "object",
            "properties": {
                "registry_path": {"type": "string",
                                  "description": "Registry JSON path (default $HERMES_HOME/tasks/tasks.json)."},
                "agent": {"type": "string", "description": "Optional: push one agent only."},
                "no_todo": {"type": "boolean", "description": "Skip the to-do list, update input_text only."},
            },
            "required": [],
        },
    },
}

HANDLERS = {
    "fleet_comms_decide": fleet_comms_decide,
    "fleet_comms_send": fleet_comms_send,
    "fleet_comms_state": fleet_comms_state,
    "fleet_comms_status": fleet_comms_status,
    "fleet_tasks_push": fleet_tasks_push,
}

DESCRIPTIONS = {
    "fleet_comms_decide": "Ask the message gate for a verdict without recording it",
    "fleet_comms_send": "Decide, record, and deliver-or-silence through the gate",
    "fleet_comms_state": "Ack, park or snooze one subject key",
    "fleet_comms_status": "Read what the gate has already said and what it is holding",
    "fleet_tasks_push": "Mirror the task registry to Home Assistant",
}


def register_tools(ctx: Any) -> None:
    """Register all five tools on the host plugin context."""
    for name, schema in SCHEMAS.items():
        ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema=schema,
            handler=HANDLERS[name],
            description=DESCRIPTIONS[name],
            emoji="📣",
        )
