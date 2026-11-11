# Catalog PR description (draft)

Suggested PR title:

    plugin-catalog: add household-fleet (message gate + Home Assistant task mirror)

Body:

---

Adds one entry: `plugin-catalog/household-fleet.yaml`.
Pinned to `<sha>` (v0.1.0), release owner: `rishiruben`.

**What it does.** A multi-agent setup produces a stream of individually reasonable
messages that add up to noise: the same reminder twice, a status report carrying no
decision, a cron wake that speaks because its slot arrived. This plugin makes the
send/no-send decision mechanical, and puts open work on a board instead of re-sending it.

- A **message gate** with nine ordered checks, first refusal wins: rollout switch, quiet
  hours (a tier-1 safety alert may break them), park/snooze, same-subject-same-facts,
  acknowledged-with-no-change, nudge ladder, per-agent / per-fleet daily caps, coalescing
  window, and a value test. Refusals are a first-class outcome and every verdict — allowed
  or refused — is appended to an auditable JSONL history.
- A **task-registry mirror** to Home Assistant: a compact per-agent summary into
  `input_text.priority_<agent>` and a reconciled native to-do list at `todo.tasks_<agent>`.

**Policy in code, not in a prompt.** The anti-repeat rule is a state hash, so a reworded
repeat of unchanged facts is still refused. Tiers, caps, quiet hours, the ladder and
ack/park/snooze are enforced by `gate.decide()` — which is pure and takes the clock and the
ledger as arguments, so the whole policy is unit-testable offline.

**Capabilities** (matches what `register()` actually does at the pinned SHA):
five tools (`fleet_comms_decide`, `fleet_comms_send`, `fleet_comms_state`,
`fleet_comms_status`, `fleet_tasks_push`), no hooks, no middleware, five optional env vars
(`FLEET_COMMS_HOME`, `FLEET_COMMS_CONFIG`, `FLEET_TASKS_FILE`, `HASS_URL`, `HASS_TOKEN`).

**Disclosure (rule 13).**

- Network: `fleet_tasks_push` makes outbound HTTPS calls to the configured Home Assistant
  URL. Nothing else in the plugin touches the network.
- Reads outside its own data: the task-registry JSON at `FLEET_TASKS_FILE` (default
  `$HERMES_HOME/tasks/tasks.json`).
- Credentials: only `HASS_TOKEN` / `HASS_URL`, read via `agent.secret_scope.get_secret`.
  No `.env` files, token files or credential stores are read directly (rule 11).
- No shell commands, no background processes, no self-updater (rule 3), no telemetry.
- No core rebinding; only `ctx.register_tool` and `ctx.register_system_prompt_section`.

**Checks run locally before opening this PR.**

- `hermes plugins validate <dir>` → green (manifest, capability probe, declared tools,
  security scan, no core override).
- `python3 -m pytest` → 40 passed, 1 skipped (the skip is an opt-in live Home Assistant
  round-trip that needs `HASS_TOKEN`).
- No Python dependencies; stdlib only.

The bundled `POLICY.md` uses no household names, clinical, financial or personal examples.
