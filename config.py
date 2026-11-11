"""Configuration for the household-fleet gate.

Everything is configurable and nothing is personal: the gate reads JSON from
``$FLEET_COMMS_CONFIG`` (default ``$FLEET_COMMS_HOME/config.json``, itself defaulting to
``$HERMES_HOME/fleet/comms``) and falls back to the documented defaults. No household names,
chat ids or addresses are baked into this plugin.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# A message body that starts with one of these AND carries no number and no question mark is
# value-free ("just checking in", "no update") and is rejected by the gate.
DEFAULT_VALUE_TEST_PATTERN = (
    r"^(reminder|just checking|still waiting|no update|friendly reminder|following up|checking in)\b"
)


def hermes_home() -> Path:
    """Resolve the Hermes home for the active profile (``$HERMES_HOME`` first)."""
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def comms_home() -> Path:
    """Directory holding ``ledger.json``, ``ledger.jsonl`` and the digest queue."""
    raw = os.environ.get("FLEET_COMMS_HOME")
    return Path(raw).expanduser() if raw else hermes_home() / "fleet" / "comms"


def config_path() -> Path:
    raw = os.environ.get("FLEET_COMMS_CONFIG")
    return Path(raw).expanduser() if raw else comms_home() / "config.json"


def tasks_file() -> Path:
    raw = os.environ.get("FLEET_TASKS_FILE")
    return Path(raw).expanduser() if raw else hermes_home() / "tasks" / "tasks.json"


@dataclass
class GateConfig:
    """Gate policy. Defaults are the ones the fleet runs in production."""

    enabled: bool = True
    timezone: str = "America/New_York"
    quiet_hours_start: str = "22:00"
    quiet_hours_end: str = "07:00"
    # Coalescing window: an agent may not send a second message inside this many minutes.
    coalesce_minutes: int = 30
    # Daily budgets.
    t2_per_agent_per_day: int = 2
    t2_per_fleet_per_day: int = 4
    t3_lines_per_agent_per_day: int = 2
    t3_lines_per_digest: int = 8
    # Nudge ladder: minimum gap before nudge 2, then before nudge 3.
    ladder_hours: List[int] = field(default_factory=lambda: [48, 168])
    # After this many unanswered nudges the key is auto-parked.
    auto_park_after_nudges: int = 3
    auto_park_days: int = 7
    value_test_pattern: str = DEFAULT_VALUE_TEST_PATTERN

    @classmethod
    def from_mapping(cls, data: Optional[Dict[str, Any]]) -> "GateConfig":
        data = data or {}
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "GateConfig":
        p = Path(path) if path else config_path()
        if not p.exists():
            return cls()
        try:
            return cls.from_mapping(json.loads(p.read_text()))
        except (OSError, json.JSONDecodeError):
            # A corrupt config must never take the gate down; run on defaults.
            return cls()

    def save(self, path: Optional[Path] = None) -> None:
        p = Path(path) if path else config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.as_dict(), indent=2) + "\n")

    def as_dict(self) -> Dict[str, Any]:
        return {f: getattr(self, f) for f in self.__dataclass_fields__}  # type: ignore[attr-defined]
