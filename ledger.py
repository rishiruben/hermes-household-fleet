"""The reminder ledger: how the gate remembers what was already said.

Two files under ``$FLEET_COMMS_HOME``:

* ``ledger.json``  — one record per *subject key* (current state: hash, tier, send_count,
  acked_at, parked_until, snooze_until).
* ``ledger.jsonl`` — append-only history of every decision, sends *and* suppressions, so
  "why was this message not delivered?" is answerable after the fact.

The ledger is the machine-enforced half of the anti-repeat rule (R3): the same key with the
same ``state_hash`` can never be sent twice, no matter what an agent wants.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .config import comms_home

ISO = "%Y-%m-%dT%H:%M:%SZ"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(ISO)


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value, ISO).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def state_hash(state: str) -> str:
    """Hash the salient facts of a message.

    Callers pass the *facts* ("draw booked 2026-11-03"), not the prose, so a reworded message
    about unchanged facts is still recognised as a repeat.
    """
    normalized = " ".join((state or "").strip().lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


@dataclass
class KeyRecord:
    """Per-subject state."""

    key: str
    tier: int = 2
    state_hash: str = ""
    summary: str = ""
    first_sent_at: Optional[str] = None
    last_sent_at: Optional[str] = None
    send_count: int = 0
    acked_at: Optional[str] = None
    parked_until: Optional[str] = None
    snooze_until: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "KeyRecord":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Ledger:
    """Load/save the ledger plus the append-only decision history."""

    home: Path = field(default_factory=comms_home)
    records: Dict[str, KeyRecord] = field(default_factory=dict)
    history: List[Dict[str, Any]] = field(default_factory=list)

    # -- paths -------------------------------------------------------------------------------
    @property
    def json_path(self) -> Path:
        return self.home / "ledger.json"

    @property
    def jsonl_path(self) -> Path:
        return self.home / "ledger.jsonl"

    @property
    def digest_path(self) -> Path:
        return self.home / "digest_queue.jsonl"

    # -- load / save -------------------------------------------------------------------------
    @classmethod
    def load(cls, home: Optional[Path] = None) -> "Ledger":
        home = Path(home) if home else comms_home()
        led = cls(home=home)
        if led.json_path.exists():
            try:
                raw = json.loads(led.json_path.read_text())
                led.records = {
                    k: KeyRecord.from_dict(v) for k, v in (raw.get("keys") or {}).items()
                }
            except (OSError, json.JSONDecodeError):
                led.records = {}
        led.history = list(led.read_history(limit=2000))
        return led

    def save(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated": iso(now_utc()),
            "keys": {k: r.to_dict() for k, r in sorted(self.records.items())},
        }
        tmp = self.json_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n")
        os.replace(tmp, self.json_path)

    def read_history(self, limit: Optional[int] = None) -> Iterable[Dict[str, Any]]:
        if not self.jsonl_path.exists():
            return []
        out: List[Dict[str, Any]] = []
        for line in self.jsonl_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out[-limit:] if limit else out

    def append_history(self, entry: Dict[str, Any]) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        with self.jsonl_path.open("a") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
        self.history.append(entry)

    # -- record access -----------------------------------------------------------------------
    def get(self, key: str) -> Optional[KeyRecord]:
        return self.records.get(key)

    def ensure(self, key: str) -> KeyRecord:
        rec = self.records.get(key)
        if rec is None:
            rec = KeyRecord(key=key)
            self.records[key] = rec
        return rec

    # -- mutations ---------------------------------------------------------------------------
    def mark_sent(self, key: str, tier: int, state: str, summary: str, when: Optional[datetime] = None) -> KeyRecord:
        when = when or now_utc()
        rec = self.ensure(key)
        if rec.send_count == 0 or rec.first_sent_at is None:
            rec.first_sent_at = iso(when)
        rec.tier = tier
        rec.state_hash = state_hash(state)
        rec.summary = summary[:100]
        rec.last_sent_at = iso(when)
        rec.send_count += 1
        return rec

    def ack(self, key: str, when: Optional[datetime] = None) -> KeyRecord:
        """Rishi replied / the item was touched: silence this key and reset the ladder."""
        rec = self.ensure(key)
        rec.acked_at = iso(when or now_utc())
        rec.send_count = 0
        rec.parked_until = None
        return rec

    def park(self, key: str, until: datetime) -> KeyRecord:
        rec = self.ensure(key)
        rec.parked_until = iso(until)
        return rec

    def snooze(self, key: str, until: datetime) -> KeyRecord:
        rec = self.ensure(key)
        rec.snooze_until = iso(until)
        rec.send_count = 0
        return rec

    # -- queries -----------------------------------------------------------------------------
    def sends_between(
        self,
        start: datetime,
        end: datetime,
        *,
        tier: Optional[int] = None,
        agent: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        out = []
        for entry in self.history:
            if not entry.get("sent"):
                continue
            if tier is not None and entry.get("tier") != tier:
                continue
            if agent is not None and entry.get("agent") != agent:
                continue
            ts = parse_iso(entry.get("at"))
            if ts is None or not (start <= ts <= end):
                continue
            out.append(entry)
        return out

    def last_send_at(self, agent: str) -> Optional[datetime]:
        stamps = [
            parse_iso(e.get("at"))
            for e in self.history
            if e.get("sent") and e.get("agent") == agent
        ]
        stamps = [s for s in stamps if s is not None]
        return max(stamps) if stamps else None

    def digest_lines(self, day_start: datetime, agent: Optional[str] = None) -> List[Dict[str, Any]]:
        if not self.digest_path.exists():
            return []
        out = []
        for line in self.digest_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = parse_iso(entry.get("at"))
            if ts is None or ts < day_start:
                continue
            if agent is not None and entry.get("agent") != agent:
                continue
            out.append(entry)
        return out

    def queue_digest_line(self, agent: str, line: str, when: Optional[datetime] = None) -> Dict[str, Any]:
        self.home.mkdir(parents=True, exist_ok=True)
        entry = {"at": iso(when or now_utc()), "agent": agent, "line": line[:200]}
        with self.digest_path.open("a") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
        return entry

    def active_keys(self) -> List[str]:
        """Keys not parked or snoozed — what the board should show as live."""
        when = now_utc()
        out = []
        for key, rec in self.records.items():
            if parse_iso(rec.parked_until) and parse_iso(rec.parked_until) > when:  # type: ignore[operator]
                continue
            if parse_iso(rec.snooze_until) and parse_iso(rec.snooze_until) > when:  # type: ignore[operator]
                continue
            out.append(key)
        return sorted(out)


def days_ago(days: int) -> datetime:
    return now_utc() - timedelta(days=days)
