"""The one piece of standing context the gate needs in every session.

Static on purpose: a dynamic state block belongs in a ``pre_llm_call`` hook (the fleet's own
deployment does that), whereas a system-prompt section is frozen into the session prompt once.
What must always be present is the *rule*, plus a pointer to ``fleet_comms_status`` which reads
the live state on demand.
"""

from __future__ import annotations

from typing import Any

SECTION_ID = "household-fleet-gate"
MAX_CHARS = 1_600

SECTION = """\
## Household messaging (household-fleet gate)

Silence is the default. A message to a person is justified only if they must decide, answer or
act, or if a safety condition is live. A scheduled wake is a prompt to CHECK, never an obligation
to speak — most wakes end with no message at all.

Before sending anything:
1. Call `fleet_comms_status` to see what has already been said today and what is parked. If
   nothing changed for that subject, say nothing.
2. Call `fleet_comms_send` with `agent`, `key` (the stable SUBJECT — `task:<id>` or a slug), the
   salient `state` facts, and `tier` (1 = safety/interrupt, 2 = needs a decision today, 3 = digest
   line, never a DM). It returns `deliver: true/false`.
3. Deliver the text yourself only when `deliver` is true. When it is false, the refusal is logged
   and auditable — that is the correct outcome, not a failure to work around.
4. When someone replies or the item is touched, call `fleet_comms_state` with `action: "ack"` so
   the ladder resets and the reminders stop. Use `park` when you have nudged twice with no answer:
   the board carries it from there.

Never repeat a subject whose facts are unchanged. Never reword a repeat to slip past the gate.
"""


def register_prompt(ctx: Any) -> None:
    """Register the gate's operating rule as a bounded system-prompt section."""
    ctx.register_system_prompt_section(SECTION_ID, SECTION, position="after_memory", max_chars=MAX_CHARS)
