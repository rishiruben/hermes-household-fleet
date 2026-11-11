"""The message gate: what is allowed to reach a person, decided in code.

The policy is one sentence — *silence is the default; a message must be earned* — and the nine
checks below are the whole implementation of it. They run in this order, and the first refusal
wins, so a suppression always has a single, nameable reason:

1. rollout switch off                       -> ``disabled``
2. quiet hours                              -> ``quiet_hours`` (safety tier may break them)
3. key parked or snoozed                    -> ``parked`` / ``snoozed``
4. same key + same state_hash               -> ``unchanged``      (the machine-enforced R3)
5. acknowledged and no new state given      -> ``acked_no_change``
6. nudge ladder not yet due                 -> ``ladder``
7. daily budget spent (per agent, per fleet) -> ``cap_agent`` / ``cap_fleet``
8. coalescing window (agent spoke recently)  -> ``coalesced``
9. value test: no number, no question mark  -> ``value_free``

Deliberately dependency-free and pure: ``decide()`` takes the ledger and the clock as arguments,
so the whole policy is testable without a running Hermes, a network, or a real clock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from .config import GateConfig
from .ledger import Ledger, iso, now_utc, parse_iso, state_hash

TIER_INTERRUPT = 1
TIER_TODAY = 2
TIER_DIGEST = 3


def _zone(name: str):
    try:
        return ZoneInfo(name)
    except Exception:
        # A missing tzdata must not stop the gate; fall back to UTC and let the reason string say so.
        return ZoneInfo("UTC")


def _parse_hhmm(value: str) -> time:
    hh, _, mm = value.partition(":")
    return time(int(hh or 0), int(mm or 0))


def in_quiet_hours(now: datetime, config: GateConfig) -> bool:
    """True inside the quiet window, which may wrap midnight (22:00 -> 07:00)."""
    local = now.astimezone(_zone(config.timezone))
    start = _parse_hhmm(config.quiet_hours_start)
    end = _parse_hhmm(config.quiet_hours_end)
    t = local.time()
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def day_start(now: datetime, config: GateConfig) -> datetime:
    """Midnight local for the config's timezone, as a UTC instant."""
    local = now.astimezone(_zone(config.timezone))
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(now.tzinfo or ZoneInfo("UTC"))


@dataclass
class MessageRequest:
    agent: str
    key: str
    text: str
    tier: int = TIER_TODAY
    state: str = ""
    safety: bool = False

    def summary(self) -> str:
        return " ".join(self.text.split())[:100]


@dataclass
class Decision:
    allowed: bool
    code: str
    reason: str
    agent: str
    key: str
    tier: int
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "code": self.code,
            "reason": self.reason,
            "agent": self.agent,
            "key": self.key,
            "tier": self.tier,
            "detail": self.detail,
        }


def _value_free(text: str, config: GateConfig) -> bool:
    """A message with no number and no question mark is a status report, not an ask."""
    if re.search(r"\d", text) or "?" in text:
        return False
    return bool(re.search(config.value_test_pattern, text.strip(), re.IGNORECASE))


def decide(
    request: MessageRequest,
    ledger: Ledger,
    config: Optional[GateConfig] = None,
    *,
    now: Optional[datetime] = None,
) -> Decision:
    """Run the nine checks and return the verdict. Pure: reads nothing, writes nothing."""
    config = config or GateConfig()
    now = now or now_utc()
    rec = ledger.get(request.key)

    def deny(code: str, reason: str, **detail: Any) -> Decision:
        return Decision(False, code, reason, request.agent, request.key, request.tier, detail)

    def allow(**detail: Any) -> Decision:
        return Decision(True, "allowed", "allowed", request.agent, request.key, request.tier, detail)

    # 1. rollout switch
    if not config.enabled:
        return deny("disabled", "the gate is switched off (enabled: false)")

    # 2. quiet hours (a safety alert may break them; nothing else may)
    if in_quiet_hours(now, config) and not (request.tier == TIER_INTERRUPT and request.safety):
        return deny("quiet_hours", f"quiet hours {config.quiet_hours_start}-{config.quiet_hours_end} "
                                   f"{config.timezone}")

    # 3. parked / snoozed
    if rec is not None:
        parked = parse_iso(rec.parked_until)
        if parked and parked > now:
            return deny("parked", f"key is parked until {rec.parked_until}", parked_until=rec.parked_until)
        snoozed = parse_iso(rec.snooze_until)
        if snoozed and snoozed > now:
            return deny("snoozed", f"key is snoozed until {rec.snooze_until}", snooze_until=rec.snooze_until)

    # 4. the hard anti-repeat: same subject, same facts, never twice
    if rec is not None and request.state:
        if rec.state_hash and rec.state_hash == state_hash(request.state):
            return deny("unchanged", "this key was already sent with the same state",
                        send_count=rec.send_count, last_sent_at=rec.last_sent_at)

    # 5. acknowledged, and no new facts stated -> stay silent until something changes
    if rec is not None and rec.acked_at and not request.state:
        return deny("acked_no_change", f"key was acknowledged at {rec.acked_at}; no new state given",
                    acked_at=rec.acked_at)

    # 6. nudge ladder
    if rec is not None and rec.send_count and rec.last_sent_at:
        last = parse_iso(rec.last_sent_at)
        ladder = list(config.ladder_hours or [])
        if last and ladder:
            if rec.send_count >= config.auto_park_after_nudges:
                until = now + timedelta(days=config.auto_park_days)
                return deny("ladder", f"{rec.send_count} unanswered nudges — park until {iso(until)}",
                            auto_park_until=iso(until), send_count=rec.send_count)
            required = ladder[min(rec.send_count - 1, len(ladder) - 1)]
            due = last + timedelta(hours=required)
            if now < due:
                return deny("ladder", f"nudge {rec.send_count + 1} is not due until {iso(due)} "
                                      f"({required} h after the last one)",
                            due_at=iso(due), send_count=rec.send_count)

    # 7. daily budgets
    if request.tier == TIER_TODAY:
        start = day_start(now, config)
        mine = ledger.sends_between(start, now, tier=TIER_TODAY, agent=request.agent)
        if len(mine) >= config.t2_per_agent_per_day:
            return deny("cap_agent", f"{request.agent} already sent {len(mine)} today "
                                     f"(cap {config.t2_per_agent_per_day})", sent_today=len(mine))
        fleet = ledger.sends_between(start, now, tier=TIER_TODAY)
        if len(fleet) >= config.t2_per_fleet_per_day:
            return deny("cap_fleet", f"the fleet already sent {len(fleet)} today "
                                     f"(cap {config.t2_per_fleet_per_day})", sent_today=len(fleet))

    # 8. coalescing: one agent, one message per window
    last_agent_send = ledger.last_send_at(request.agent)
    if last_agent_send is not None and config.coalesce_minutes > 0:
        gap = (now - last_agent_send).total_seconds() / 60.0
        if gap < config.coalesce_minutes:
            return deny("coalesced", f"{request.agent} spoke {gap:.0f} min ago "
                                     f"(coalescing window {config.coalesce_minutes} min)",
                        minutes_since_last=round(gap, 1))

    # 9. value test
    if _value_free(request.text, config):
        return deny("value_free", "no number and no question — a status report, not an ask")

    return allow(state_hash=state_hash(request.state), text_length=len(request.text))


def record_decision(
    decision: Decision,
    request: MessageRequest,
    ledger: Ledger,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Apply a verdict to the ledger and append it to history. Every decision is auditable."""
    now = now or now_utc()
    if decision.allowed:
        ledger.mark_sent(request.key, request.tier, request.state, request.summary(), when=now)
    elif decision.code == "ladder" and decision.detail.get("auto_park_until"):
        ledger.park(request.key, parse_iso(decision.detail["auto_park_until"]) or now)  # type: ignore[arg-type]
    ledger.save()
    entry = {
        "at": iso(now),
        "agent": request.agent,
        "tier": request.tier,
        "key": request.key,
        "sent": decision.allowed,
        "code": decision.code,
        "state_hash": state_hash(request.state),
        "summary": request.summary(),
    }
    ledger.append_history(entry)
    return entry


def decide_digest_line(
    agent: str,
    line: str,
    ledger: Ledger,
    config: Optional[GateConfig] = None,
    *,
    now: Optional[datetime] = None,
) -> Decision:
    """Digest lines are informative only: never a DM, capped per agent and per digest."""
    config = config or GateConfig()
    now = now or now_utc()
    start = day_start(now, config)
    request = MessageRequest(agent=agent, key=f"digest:{agent}", text=line, tier=TIER_DIGEST)
    if not config.enabled:
        return Decision(False, "disabled", "the gate is switched off", agent, request.key, TIER_DIGEST)
    mine = ledger.digest_lines(start, agent=agent)
    if len(mine) >= config.t3_lines_per_agent_per_day:
        return Decision(False, "cap_agent", f"{agent} already queued {len(mine)} digest lines today "
                                            f"(cap {config.t3_lines_per_agent_per_day})",
                        agent, request.key, TIER_DIGEST, {"queued_today": len(mine)})
    all_lines = ledger.digest_lines(start)
    if len(all_lines) >= config.t3_lines_per_digest:
        return Decision(False, "cap_digest", f"the digest already holds {len(all_lines)} lines "
                                             f"(cap {config.t3_lines_per_digest})",
                        agent, request.key, TIER_DIGEST, {"queued_today": len(all_lines)})
    if _value_free(line, config):
        return Decision(False, "value_free", "no number and no question — nothing changed", agent,
                        request.key, TIER_DIGEST)
    ledger.queue_digest_line(agent, line, when=now)
    return Decision(True, "allowed", "queued for the next digest", agent, request.key, TIER_DIGEST,
                    {"line": line[:200]})
