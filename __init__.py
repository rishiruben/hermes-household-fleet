"""hermes-household-fleet — a message gate and a task mirror for a multi-agent household.

A household running several agents produces a stream of well-meaning, individually reasonable
messages that add up to noise: the same reminder twice, a "status report" that carries no
decision, a scheduled wake that speaks because its cron slot arrived rather than because anything
changed. This plugin makes silence the default and the decision mechanical:

* **The gate** (``gate.py`` + ``ledger.py``) answers one question — *may this message be sent?* —
  with nine ordered checks (rollout switch, quiet hours, parked/snoozed, same key + same state,
  acknowledged with no new facts, nudge ladder, daily caps, coalescing window, value test). The
  same subject in the same state can never be sent twice, because that is enforced by a state
  hash, not by an agent's good intentions. Every verdict, allowed or refused, is appended to an
  auditable JSONL history, so "why was this not delivered?" always has an answer.
* **The mirror** (``ha_mirror.py``) pushes a task registry to Home Assistant: a compact summary
  into ``input_text.priority_<agent>`` and a reconciled native to-do list at ``todo.tasks_<agent>``,
  so unacknowledged items live on a board instead of being re-sent.

Delivery is deliberately *not* this plugin's job: the gate decides, records, and returns the text;
the host agent delivers it on whatever channel it owns. That keeps credentials, tokens and chat
ids out of the plugin, and makes the whole policy unit-testable with no network and no clock.

Wiring goes only through the public plugin API (``ctx.register_tool`` /
``ctx.register_system_prompt_section``). The submodule imports are deliberately inside
``register()``: importing this package must have no side effects, and no relative import runs
until the host actually loads the plugin.
"""

from __future__ import annotations

from typing import Any

__version__ = "0.1.0"


def register(ctx: Any) -> None:
    """Plugin entry point — called by the Hermes plugin loader."""
    from .prompt import register_prompt
    from .tools import register_tools

    register_tools(ctx)
    register_prompt(ctx)
