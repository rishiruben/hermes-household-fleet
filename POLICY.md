# A Signal-Over-Schedule Messaging Policy

This is the reasoning behind the gate in `gate.py`. It is written to be dropped into any
multi-agent setup: replace "household" with whatever grouping the agents serve, and set the
quiet window, caps and timezone in config.

## The diagnosis

Observations from a real multi-agent deployment, over five days: the agents sent roughly
8–12 unsolicited messages per day, every one individually defensible and collectively
noise. Three failure modes account for essentially all of it:

1. **Repeat.** The same item is re-sent because no agent holds a memory of what it already
   said, or whether anyone saw it.
2. **Schedule-driven.** A job speaks because its cron slot arrived, not because anything
   changed.
3. **Value-free.** A status report that carries no decision and no action — "still
   pending", "no update yet", "just checking in".

## The rules

| # | Rule |
|---|---|
| R1 | **Silence is the default.** A message is justified only if someone must decide, answer or act — or if a safety condition is live. Everything else is logged, not spoken. |
| R2 | **A scheduled wake is a prompt to check, never an obligation to speak.** Most wakes end with "nothing changed → say nothing". |
| R3 | **Never send the same thing twice.** A message whose subject and state are unchanged is suppressed **in code**, not by agent discretion. |
| R4 | **Acknowledgement silences.** Once the recipient replies, or touches the item, further reminders stop until the state changes or the deadline enters the last 24 hours. |
| R5 | **When it repeats, it must carry news.** A re-send is allowed only if it states *what changed* — never a re-explanation of the background. |
| R6 | **Backoff, then park.** Unacknowledged items escalate on a fixed ladder, then get parked on the board as "waiting on you" instead of continuing to ping. |
| R7 | **One thought per message.** No bundling of unrelated asks into a status wall. The only allowed bundling is the daily digest. |
| R8 | **Quantify and offer the next action.** Numbers, dates, amounts — plus a default action, so a one-word answer closes it. |
| R9 | **Two unanswered nudges means stop.** Nagging is not a strategy; the board is the reminder surface. |
| R10 | **Quiet hours are absolute**, except a genuine safety alert. |

## Tiers — what is allowed to reach a person

| Tier | Definition | Delivery | Budget |
|---|---|---|---|
| **T1 Interrupt** | Safety or time-critical: a hazard, a payment due or overdue today, a hard deadline inside 24 h, something broken that needs the person, a blocker stopping other agents' work. | immediate | uncapped, but audited — a sustained high rate triggers a false-positive review |
| **T1s Safety** | The subset of T1 that may break quiet hours (safety only). | immediate | uncapped; expected ~0/week |
| **T2 Today** | Needs a decision today, or the state changed on something the person is waiting for; deadline within 72 h. | next allowed window, never inside quiet hours | ≤ per-agent and per-fleet daily caps |
| **T3 Digest** | Informative, on-track, "FYI", periodic reviews. | one scheduled digest | capped per agent and per digest |
| **T4 Silent** | Everything else. | ledger and board only | — |

Two mechanical caps sit on top of the tiers:

- **Coalescing window:** no agent may send a second message within N minutes of its own
  previous one; the second thing waits, or folds into the same message if it is the same
  subject.
- **Value test (auto-reject):** a body that starts with `reminder`, `just checking`,
  `still waiting`, `no update`, `friendly reminder` or `following up` and carries no digit
  and no question mark is rejected as value-free. A number or a question is evidence that
  the message wants something.

## The reminder ledger

One record per **subject key** — the stable identity of the subject, not of the message. A
task id, or a slug for a standing subject; the same subject uses the same key across agents.
Each record holds the state hash, tier, first and last send time, send count, and the
acknowledgement / park / snooze timestamps. An append-only history records every verdict,
sends and suppressions alike.

The records carry four behaviours:

- **Dedupe (R3).** A key whose state hash is unchanged cannot be sent again, whatever the
  agent wants. The hash covers the *facts* passed as `state`, so a reworded message about
  unchanged facts is still a repeat.
- **Ladder and park (R6/R9).** Nudges 2 and 3 need a minimum gap; after the third
  unanswered nudge the key is auto-parked for a fixed period and the board carries it.
- **Acknowledgement (R4).** A reply silences every key that agent sent in the previous 48
  hours and resets the ladder, so one genuine state change afterwards is deliverable
  immediately. A change to the underlying item also counts as an acknowledgement.
- **Snooze / park.** "Later" sets a snooze date; a one-word answer closes the item.

## The daily digest

Instead of each job reporting on its own schedule, informative lines are queued and flushed
once a day in a single brief, grouped by domain, each line *what changed + what it needs*.
If no agent submitted a line, no digest is sent. Silence is a valid outcome.

## Job classes

Every scheduled job is classified, and the class decides whether it may ever speak:

| Class | Behaviour |
|---|---|
| `ALERT` | Runs to detect a condition; speaks only when the condition is true (T1/T2). |
| `CHECK` | Wakes the agent to review state; speaks only on a delta. Most wakes are silent. |
| `DIGEST` | Produces digest lines; never sends on its own. |
| `NONE` | Plumbing (syncs, pushes, audits). Silent on success, alerts on failure. |

A `CHECK`/`ALERT` job can also carry a deterministic **monitor**: a snapshot of the agent's
meaningful local state. If the snapshot is identical to the previous tick the agent is not
run at all — no model call, no message, no cost.

Two binding rules follow: **no job may deliver to the messaging surface on its own** — the
gate is the only send path — and every prompt is written in check→decide form ("if the state
changed and the person must act, send via the gate; otherwise say nothing").

## Metrics

| Metric | Measured baseline | Target |
|---|---|---|
| Unsolicited messages/day | 8–12 | ≤2 median |
| Repeat sends (same key, unchanged state) | visibly present | 0 (hard gate, audited) |
| Separate reports/day | 4–6 | 1 digest |
| Urgent-alert latency | — | < 5 min |
| Reminders after an acknowledgement | — | 0 until a state change |
| Parked items shown on the board | 0 | ≥1 whenever two nudges went unanswered |

## Voice

| Bad | Good |
|---|---|
| "Reminder: prior authorisation still outstanding. Status: pending. Action needed: follow up." | "The insurer still hasn't answered the prior authorisation, asked Tuesday. Want me to call them today?" |
| "Weekly home ops update: 6 items reviewed, 2 on track, 4 upcoming…" | "The filter wash held the air quality flat — no action. The water heater service is due 2026-11-02; shall I book it?" |
| "No update on the loan documents." | *(nothing — the state is unchanged, so silence)* |
| "Just checking in on today's workout." | "Legs today per plan, 3×5 back squat at 100 kg. Logged?" |
