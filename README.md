# household-fleet

Two primitives a multi-agent household needs and Hermes does not ship: a **message gate**
that makes silence the default, and a **task mirror** that puts open work on a Home
Assistant board instead of re-sending it.

A household running several agents produces a stream of individually reasonable messages
that add up to noise — the same reminder twice, a "status report" carrying no decision, a
scheduled wake that speaks because its cron slot arrived rather than because anything
changed. This plugin makes the send/no-send decision *mechanical* instead of a matter of
an agent's good intentions, and makes every decision auditable.

```
                       ┌──────────────────────── plugin boundary ───────────────────────┐
  agent turn ──► fleet_comms_send ──► gate.decide()  ──► ledger (json + jsonl)          │
                     ▲                 9 ordered checks        │                         │
                     │                 first refusal wins      │ allowed                 │
  fleet_comms_status ┘                                        ▼                         │
                                                     deliver: true  ──► the host agent
                                                                        delivers on its
                                                                        own channel
  tasks.json ──► fleet_tasks_push ──► ha_mirror ──► input_text.priority_<agent>
                                                └─► todo.tasks_<agent>
```

## What it does

**The gate** (`gate.py`, `ledger.py`) answers one question — *may this message be sent?* —
with nine ordered checks. The first refusal wins, so a suppression always has one nameable
reason:

| # | Check | Refusal code |
|---|---|---|
| 1 | rollout switch off | `disabled` |
| 2 | quiet hours (a tier-1 *safety* alert may break them) | `quiet_hours` |
| 3 | the subject key is parked or snoozed | `parked` / `snoozed` |
| 4 | same key **and** same state hash → already said | `unchanged` |
| 5 | acknowledged and no new facts stated | `acked_no_change` |
| 6 | nudge ladder not yet due; after N unanswered nudges, auto-park | `ladder` |
| 7 | daily budget spent (per agent, per fleet) | `cap_agent` / `cap_fleet` |
| 8 | this agent spoke inside the coalescing window | `coalesced` |
| 9 | no number and no question — a status report, not an ask | `value_free` |

Check 4 is the one that makes the gate worth having: the *facts* are hashed, not the prose,
so a reworded repeat of an unchanged subject is still refused. Rewording cannot slip past
it. The ledger keeps a per-subject record (state hash, tier, send count, ack, park, snooze)
and an append-only `ledger.jsonl` of **every** verdict — allowed *and* refused — so "why
was this not delivered?" is answerable months later.

**The mirror** (`ha_mirror.py`) pushes a task registry to Home Assistant: a compact
per-agent summary into `input_text.priority_<agent>`, and a reconciled native to-do list at
`todo.tasks_<agent>` (idempotent add/update/remove via `todo.get_items` / `add_item` /
`update_item` / `remove_item`). A missing to-do entity is reported, not raised — the text
summary still lands.

Delivery is deliberately *not* this plugin's job. The gate decides, records and returns the
text; the host agent delivers it on whatever channel it owns. That keeps tokens, chat ids
and network code out of the policy, and makes the whole thing unit-testable with no network
and no real clock (`decide()` takes the ledger and the clock as arguments).

## Install

```bash
hermes plugins install rishiruben/hermes-household-fleet   # from the catalog
# or from a checkout:
hermes plugins install ~/workspace/hermes-household-fleet
hermes plugins validate ~/workspace/hermes-household-fleet   # offline admission checks
```

Requires Hermes `>=0.21`. No Python dependencies — stdlib only.

## Run the tests

```bash
cd hermes-household-fleet
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest          # 40 passed, 1 skipped (needs HASS_TOKEN)
```

The one skipped test is an opt-in live Home Assistant round-trip; everything else runs
offline and deterministic.

## Tools

| Tool | Purpose |
|---|---|
| `fleet_comms_decide` | Ask the gate for a verdict *without* recording anything. |
| `fleet_comms_send` | The sanctioned send path: decide, record, return `deliver: true/false`. Tier 3 queues a digest line instead of sending. |
| `fleet_comms_state` | `ack` (the person replied — silence the key, reset the ladder), `park` (stop nudging), `snooze` (remind me later). |
| `fleet_comms_status` | Read what has already been said today, each cap, quiet-hours state, every parked key. **Call this first on any scheduled wake** — most wakes should end in silence. |
| `fleet_tasks_push` | Mirror the task registry to Home Assistant. |

The plugin also registers one bounded system-prompt section carrying the operating rule,
so the policy applies to old sessions and cron turns alike.

## Configuration

`config_schema` (editable in the Desktop settings form) covers the rollout switch, the
timezone, the quiet-hours window, the coalescing window, the per-agent / per-fleet daily
caps, the digest caps, the nudge ladder and the auto-park threshold. Defaults:
`22:00–07:00` local, 30-minute coalescing, 2 T2 per agent per day, 4 per fleet, ladder
`48 h → 168 h`, auto-park after 3 unanswered nudges.

Optional environment (`requires_env`, read through `agent.secret_scope.get_secret`):

| Name | Used by |
|---|---|
| `FLEET_COMMS_HOME` | Directory holding the ledger and config (default `$HERMES_HOME/fleet/comms`). |
| `FLEET_COMMS_CONFIG` | Path to a gate `config.json`. |
| `FLEET_TASKS_FILE` | Task registry JSON to mirror (default `$HERMES_HOME/tasks/tasks.json`). |
| `HASS_URL` | Home Assistant base URL (default `http://homeassistant.local:8123`). |
| `HASS_TOKEN` | Home Assistant long-lived access token — **password** field. |

## Disclosure (catalog rule 13)

What to know before installing:

- **Network calls:** `fleet_tasks_push` makes outbound HTTPS requests to the Home
  Assistant URL you configure. Nothing else in the plugin touches the network.
- **Reads outside its own data:** it reads the task registry JSON at `FLEET_TASKS_FILE`
  (default `$HERMES_HOME/tasks/tasks.json`) and writes ledger state under
  `FLEET_COMMS_HOME`. It does not scan any other path.
- **Credentials:** only `HASS_TOKEN` / `HASS_URL`, read through Hermes's secret scope.
  No `.env` files, no token files, no credential stores are read directly.
- **Shell commands / background processes:** none. Stdlib `urllib` only.
- **Self-updating:** none, by design (catalog rule 3).
- **Telemetry:** none.

## Policy

The reasoning behind the gate — the nine rules, the tiers, the ladder, the acknowledgment
semantics — is written up in [POLICY.md](POLICY.md).

## Layout

```
__init__.py     plugin entry point (register(ctx))
plugin.yaml     manifest: tools, config schema, env
gate.py         the nine checks — pure, no clock, no network
ledger.py       per-key state + append-only decision history
config.py       GateConfig, paths, defaults
ha_mirror.py    task registry -> input_text + todo lists
tools.py        the five tool handlers and their schemas
prompt.py       the bounded system-prompt section
tests/          one test per gate check plus the interesting combinations
catalog/        the plugin-catalog entry, pinned to a commit SHA
```

## License

MIT — see [LICENSE](LICENSE).
