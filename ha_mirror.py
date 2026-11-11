"""Task-registry -> Home Assistant mirror.

Two HA surfaces, both idempotent:

* ``input_text.priority_<agent>`` — a compact per-agent summary for a dashboard.
* ``todo.tasks_<agent>``          — a native to-do list, reconciled via
  ``todo.get_items`` / ``add_item`` / ``update_item`` / ``remove_item``.

A missing to-do entity (``local_todo`` not installed, or the list not created) is not an error:
the entity is skipped and reported, and the text summary still lands. Stdlib only — no
``requests``, no ``curl`` subprocess.

Credentials come from the environment (``HASS_URL``, ``HASS_TOKEN``), resolved through
``agent.secret_scope.get_secret`` when running inside Hermes and falling back to ``os.environ``
when running standalone (tests, CLI).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib import error, request

STATUS_ICON = {"open": "○", "in_progress": "◐", "blocked": "⊗", "done": "✓"}
PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
DEFAULT_HASS_URL = "http://homeassistant.local:8123"
INPUT_TEXT_MAX = 255


def _secret(name: str, default: str = "") -> str:
    """Read a secret inside Hermes when possible, else from the plain environment."""
    try:
        from agent.secret_scope import get_secret  # type: ignore
        value = get_secret(name, default)
        if value:
            return str(value)
    except Exception:
        pass
    return os.environ.get(name, default)


@dataclass
class MirrorConfig:
    url: str = field(default_factory=lambda: (_secret("HASS_URL", DEFAULT_HASS_URL) or DEFAULT_HASS_URL).rstrip("/"))
    token: str = field(default_factory=lambda: _secret("HASS_TOKEN", ""))
    timeout: float = 12.0


class HomeAssistantError(RuntimeError):
    """A Home Assistant call failed (transport, auth, or a rejected service call)."""


def call_service(
    config: MirrorConfig,
    domain: str,
    service: str,
    payload: Dict[str, Any],
    *,
    return_response: bool = False,
) -> Dict[str, Any]:
    """POST ``<domain>.<service>`` and return the parsed body (``{}`` for a plain 200)."""
    if not config.token:
        raise HomeAssistantError(
            "HASS_TOKEN is not set — cannot reach Home Assistant. "
            "Set it in the profile's .env or the environment."
        )
    url = f"{config.url}/api/services/{domain}/{service}"
    if return_response:
        url += "?return_response"
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {config.token}")
    req.add_header("Content-Type", "application/json")
    try:
        with request.urlopen(req, timeout=config.timeout) as resp:
            raw = resp.read().decode("utf-8") or "{}"
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise HomeAssistantError(f"{domain}.{service} -> HTTP {exc.code}: {detail}") from exc
    except error.URLError as exc:
        raise HomeAssistantError(f"{domain}.{service} -> {exc.reason}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


# ─── registry reading ────────────────────────────────────────────────────────────────────────

@dataclass
class Task:
    id: str
    agent: str
    title: str
    status: str = "open"
    priority: str = "medium"
    due: Optional[str] = None
    note: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Task":
        return cls(
            id=str(data.get("id", "")),
            agent=str(data.get("agent", "")),
            title=str(data.get("title", "")).strip(),
            status=str(data.get("status", "open")),
            priority=str(data.get("priority", "medium")),
            due=data.get("due") or None,
            note=str(data.get("note", "")),
        )


def load_registry(path: Optional[Path] = None) -> Tuple[List[Task], List[str]]:
    """Read the task registry. Returns ``(tasks, agents)``; creates nothing."""
    from .config import tasks_file

    p = Path(path) if path else tasks_file()
    if not p.exists():
        raise FileNotFoundError(f"task registry not found: {p}")
    data = json.loads(p.read_text())
    tasks = [Task.from_dict(t) for t in data.get("tasks", [])]
    agents = list(data.get("agents") or sorted({t.agent for t in tasks}))
    return tasks, agents


def build_summary(agent: str, tasks: List[Task], max_chars: int = 250) -> str:
    """A compact, deterministic board summary for one agent."""
    active = [t for t in tasks if t.agent == agent and t.status != "done"]
    active.sort(key=lambda t: (PRIORITY_ORDER.get(t.priority, 2), t.due or "9999"))
    lines = [f"{agent}: {len(active)} active"]
    used = len(lines[0])
    for t in active:
        icon = STATUS_ICON.get(t.status, "?")
        row = f"{icon} {t.title}"
        if t.due:
            row += f" (due {t.due})"
        if used + len(row) + 1 > max_chars:
            lines.append(f"+{len(active) - (len(lines) - 1)} more")
            break
        lines.append(row)
        used += len(row) + 1
    return "\n".join(lines)[:INPUT_TEXT_MAX]


# ─── HA surfaces ─────────────────────────────────────────────────────────────────────────────

def get_todo_items(config: MirrorConfig, entity: str) -> Optional[List[Dict[str, Any]]]:
    """Return the entity's items, or None when the entity does not exist."""
    try:
        data = call_service(
            config, "todo", "get_items",
            {"entity_id": entity, "status": ["needs_action", "completed"]},
            return_response=True,
        )
    except HomeAssistantError:
        return None
    response = data.get("service_response") if isinstance(data, dict) else None
    if not isinstance(response, dict):
        return None
    entity_payload = response.get(entity)
    if not isinstance(entity_payload, dict):
        return None
    return [
        {
            "summary": item.get("summary", ""),
            "status": item.get("status", "needs_action"),
            "due": item.get("due"),
        }
        for item in entity_payload.get("items", [])
    ]


def sync_todo_list(config: MirrorConfig, agent: str, tasks: List[Task]) -> str:
    """Reconcile ``todo.tasks_<agent>`` with the registry. Returns a one-line report."""
    entity = f"todo.tasks_{agent}"
    desired: Dict[str, Tuple[str, Optional[str]]] = {}
    for t in tasks:
        if t.agent != agent:
            continue
        desired[t.title] = ("completed" if t.status == "done" else "needs_action", t.due)

    items = get_todo_items(config, entity)
    if items is None:
        return "skipped (no todo entity)"
    current = {i["summary"]: i for i in items}

    added = updated = removed = 0
    for title, (status, due) in desired.items():
        if title in current:
            if current[title]["status"] != status:
                payload: Dict[str, Any] = {"entity_id": entity, "item": title, "status": status}
                call_service(config, "todo", "update_item", payload)
                updated += 1
        else:
            payload = {"entity_id": entity, "item": title}
            if due:
                payload["due_date"] = due
            call_service(config, "todo", "add_item", payload)
            added += 1
    for title in current:
        if title not in desired:
            call_service(config, "todo", "remove_item", {"entity_id": entity, "item": title})
            removed += 1
    return f"{added} added, {updated} updated, {removed} removed"


def set_input_text(config: MirrorConfig, entity: str, value: str) -> None:
    call_service(config, "input_text", "set_value", {"entity_id": entity, "value": value[:INPUT_TEXT_MAX]})


def mirror(
    registry_path: Optional[Path] = None,
    *,
    agent: Optional[str] = None,
    sync_todo: bool = True,
    config: Optional[MirrorConfig] = None,
) -> Dict[str, Any]:
    """Push the task registry to Home Assistant. Never raises for a missing to-do entity."""
    config = config or MirrorConfig()
    tasks, agents = load_registry(registry_path)
    targets = [agent] if agent else agents
    results: List[Dict[str, Any]] = []
    for name in targets:
        if name not in agents:
            results.append({"agent": name, "ok": False, "error": "not in registry"})
            continue
        summary = build_summary(name, tasks)
        row: Dict[str, Any] = {"agent": name, "chars": len(summary)}
        try:
            set_input_text(config, f"input_text.priority_{name}", summary)
            row["input_text"] = "ok"
            row["ok"] = True
        except HomeAssistantError as exc:
            row["input_text"] = f"error: {exc}"
            row["ok"] = False
        if sync_todo:
            try:
                row["todo"] = sync_todo_list(config, name, tasks)
            except HomeAssistantError as exc:
                row["todo"] = f"error: {exc}"
        results.append(row)
    return {"ok": all(r.get("ok") for r in results) if results else True, "results": results}
