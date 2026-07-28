# Star Office UI — Hermes Hook Debugging

## Symptom

Star Office UI (embedded in token-dashboard middle panel) shows agent stuck
in a working state (writing/executing) even after the conversation turn ends.
The role never returns to the breakroom (idle). Waiting 30+ seconds — well
beyond the dashboard's 15-second poll interval — does not resolve it.

### Variant: Agent Never Returns to Idle

**Observed 2026-07-26.** After every conversation turn the agent stays at
the desk. Waiting >30 seconds, well past the 15-second dashboard poll
interval, shows no recovery. This is a different symptom from the original
"stuck in executing during tool loops" — the agent is truly done but the
state persists indefinitely.

## Root Cause Checklist

1. **Plugin not installed:** `star-office-ui-status` Hermes plugin was never
   installed in the dev profile.

2. **Plugin installed but not yet effective:** Plugin hook registration
   takes effect on the _next_ Hermes session. The current conversation
   (started before install) won't push data.

3. **Stuck in "executing" state:** `pre_tool_call` hook writes `executing`
   to state.json. If the subsequent event is a `post_tool_call` (not
   `post_llm_call`), the session phase may not reset to idle. The hook
   only clears phase on `post_llm_call`, `on_session_end`, `on_session_finalize`,
   or `on_session_reset`. During tool-heavy sequences without LLM calls
   between them, the state remains `executing`.

   **Diagnosis:** Check Star Office `/status` — if `state: "executing"` persists
   even when the agent appears idle, and `updated_at` keeps refreshing:
   ```
   curl http://127.0.0.1:19000/status | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['state'], d.get('updated_at',''))"
   ```

   **TTL:** Star Office's backend has a 300-second auto-idle fallback
   (in `backend/app.py`). But if the hook keeps writing new state (bumping
   `updated_at`), the TTL never triggers.

   **Quick fix:** Manually reset state.json:
   ```bash
   echo '{"state":"idle","detail":"Hermes is ready","progress":0,"activity":{"activityKind":"idle"},"updated_at":""}' \
     > ~/HermesWorkspace/Star-Office-UI-Hermes/state.json
   ```

   **Root fix (partial — insufficient on its own):** In `star_office_hook.py`, the
   `post_tool_call` event handler should reset `session["phase"] = "idle"` when
   no further tool calls are active.

4. **Orphan child-session entries permanently pollute `display_state()`**
   **(discovered 2026-07-26 by Claude Code independent diagnosis):**

   When Hermes spawns a subagent (Codex CLI), tool-call events for the child
   carry the child's session ID in the payload. `session_id()` returns this
   child ID, creating a separate `sessions[child_id]` entry in the hook state
   file. Cleanup events (`post_llm_call`, `on_session_end`) fire with the
   *parent* session ID and never touch the child entry. `subagent_stop` removes
   the child from the parent's `subagents` list but does **not** delete
   `sessions[child_id]`.

   `display_state()` iterates **all** sessions and takes the max phase/active
   state. An orphan `sessions[child_id]` with `phase: "executing"` permanently
   outranks the parent's `idle`, so the agent never returns to the breakroom.

   **Diagnosis:** Inspect `star-office-hermes-hook-state.json` (usually under
   `$TMPDIR`):
   ```bash
   find /tmp /var/folders -name "star-office-hermes-hook-state.json" 2>/dev/null
   python3 -c "import json; d=json.load(open('...')); [print(k,v['phase']) for k,v in d['sessions'].items()]"
   ```
   If sessions outnumber active conversations, orphans exist.

   **Complete fix (four changes in `star_office_hook.py`):**
   a) `subagent_stop` cleans orphan: `sessions.pop(str(child_id), None)` when
      child_id differs from current_session_id.
   b) `post_tool_call` sets phase to `"idle"` when `active` and `subagents`
      are both empty (not always `"writing"`).
   c) `on_session_start` replaces the entire session dict to clear stale tools,
      subagents, and counters from unclean shutdowns. Also split
      `on_session_start` from `pre_llm_call` — start keeps idle, LLM call
      sets writing.

   **Pitfall:** Codex may implement (c) by replacing the session dict but leave
   the old `if event in ("on_session_start", "pre_llm_call"): phase = "writing"`
   branch intact — this silently overwrites the idle. Verify with ad-hoc test.

   **Companion fix — TTL must also cover `syncing` (`backend/app.py`):**
   `WORKING_STATES` (line 87) controls which states the 300-second auto-idle
   TTL can recover. The original set `{"writing", "researching", "executing"}`
   excluded `"syncing"`. If a syncing state is pushed but the corresponding
   `post_tool_call` hook is lost (process crash, race), the animation is
   permanently stuck — TTL cannot recover it. Fixed by adding `"syncing"`:
   ```python
   WORKING_STATES = frozenset({"writing", "researching", "executing", "syncing"})
   ```

### 5. Startup State Reset

After restart, Star Office may show stale "executing" state. The
`reset_working_state_on_startup()` function in `backend/app.py` resets any
working state to idle on Flask startup. However:

- **Only takes effect on full process restart** — the service-manager must
  restart the Star Office Python process. Token-dashboard restart alone is
  not enough if Star Office's PID survived.
- **Verification commands trigger hooks** — `curl :19000/status` during
  verification runs through `terminal()` which fires `pre_tool_call →
  executing`. The state IS idle briefly after startup before any tool calls.

### 6. Manual Hook Tests Are Deceptive
   refined same day):

   When Hermes calls Codex or Claude CLI via the terminal tool, Star Office
   should play the sync animation instead of the normal executing animation.

   **First attempt (naive substring match — rejected):**
   Check `tool_input.command` for `"codex"` or `"claude"` anywhere in the
   string.  Problem: false-positives on `echo codex`, file paths, grep
   patterns, arguments; false-negatives on absolute paths, `sudo`, `env`,
   `bash -c`, and argv arrays.

   **Final implementation (shell-lexer, `command_invokes_sync_cli()`):**
   Use `shlex` to tokenize the command, then walk tokens recognizing only
   `codex`/`claude` in *executable position* (basename match).  Handles:
   - absolute paths (`/opt/tools/claude -p ...`)
   - wrappers (`command`, `exec`, `nohup`, `time`)
   - `env VAR=val codex exec` and `sudo -u nobody claude`
   - shell wrappers (`bash -c 'codex ...'`, `sh -lc 'claude ...'`)
   - pipes/separators (`;`, `&`, `|`, `&&`) — resets expect_command after each
   - argv arrays (`["codex", "exec", "review"]`)
   - Hermes `extra` envelope (`payload.extra.tool_input.command`)
   Correctly skips: `echo codex`, `cat /tmp/claude.log`,
   `python script.py --provider codex`, `my-codex-wrapper run`.

   **Key functions added to `star_office_hook.py`:**
   - `command_invokes_sync_cli(command)` — shell-lexer with executable-position matching
   - `sync_cli_command(payload)` — extracts command from payload or extra.tool_input

   **Pitfall discovered during testing:** manual hook tests
   (`echo '{"hook_event_name":"pre_tool_call",...}' | python star_office_hook.py`)
   leave permanent residues in the hook state file.  The `test` session
   with `active={'x':'syncing'}` outranks all real sessions in
   `display_state()`, causing a permanently-stuck sync animation.
   Cleanup: delete the orphan session from
   `$TMPDIR/star-office-hermes-hook-state.json` or reset `state.json`.

6. **Plugin `_payload()` not forwarding `tool_input`** (discovered 2026-07-26):

   The plugin's `__init__.py:_payload()` only copies fields listed in
   `_TOP_LEVEL_FIELDS` into the bridge payload:

   ```python
   _TOP_LEVEL_FIELDS = ("tool_name", "session_id")  # ← "tool_input" MISSING
   ```

   The bridge's `sync_cli_command(payload)` looks for `payload.tool_input.command`
   and `payload.extra.tool_input.command` — neither exists because the plugin
   never includes `tool_input`. Result: all codex/claude sync detection
   silently fails; every terminal call shows "executing".

   **Diagnosis:** Manual tests with `echo | python hook.py` inject `tool_input`
   directly and WILL PASS even when the plugin is broken. To confirm, run a
   real codex call and check the hook state file — if active values are always
   "executing" (never "syncing"), the plugin is not forwarding.

   **Fix:** Add `"tool_input"` to `_TOP_LEVEL_FIELDS`:
   ```python
   _TOP_LEVEL_FIELDS = ("tool_name", "session_id", "tool_input")
   ```
   Then **restart Hermes** (plugin Python modules are loaded at startup;
   the hook file is a subprocess per event and does not need restart).

7. **`HERMES_DESKTOP` not set:** The plugin's `register()` function gates on
   `os.environ.get("HERMES_DESKTOP") == "1"`. If this env var is absent,
   the plugin silently returns without registering any hooks.

8. **`args` ≠ `tool_input` — Hermes field name mismatch (discovered 2026-07-26):**

   Hermes Python plugin hooks pass tool arguments as **`args`**, not `tool_input`:
   ```
   kwargs = {..., "args": {"command": "codex exec ...", "pty": True}}
   ```

   The plugin's `_payload()` copies `_TOP_LEVEL_FIELDS` into the bridge payload,
   but the bridge (`star_office_hook.py`) expects `payload.tool_input.command`.
   Solution: keep `"args"` in `_TOP_LEVEL_FIELDS` and rename it during copy:
   ```python
   # In _payload():
   key = "tool_input" if field == "args" else field
   payload[key] = kwargs[field]
   ```
   This is a separate issue from item 6 (plugin not forwarding at all).
   Even with `tool_input` in `_TOP_LEVEL_FIELDS`, if the field name doesn't
   match what Hermes actually passes, detection still silently fails.

9. **`on_session_start` pushes idle state to backend (fixed 2026-07-26):**

   **Symptom:** Character animation shows idle during an active session.
   The frontend displays the idle animation even though the agent is working.

   **Root cause:** `star_office_hook.py`'s `run()` → `reserve()` unconditionally
   builds and delivers a state update on every `on_session_start` event. Since
   the hook initializes new sessions with `phase=idle`, each session start
   (including cron sessions like `cron_dadda7400df4_*`) pushes `idle` to the
   backend via `POST /set_state`, overwriting any previous working state.

   **Diagnosis — inspect hook state file for idle-pushing sessions:**
   ```bash
   HOOK_STATE=$(find /var/folders -name "star-office-hermes-hook-state.json" -not -name "*.lock" 2>/dev/null | head -1)
   python3 -c "
   import json, time
   with open('$HOOK_STATE') as f:
       d = json.load(f)
   now = time.time()
   for sid, s in sorted(d.get('sessions',{}).items()):
       ua = s.get('updated_at')
       age = f'{now-ua:.0f}s ago' if ua else 'N/A'
       print(f'{sid[-30:]:30s} phase={s.get(\"phase\"):10s} active={len(s.get(\"active\",{})):2d} age={age}')
   lp = d.get('last_push',{})
   print(f'last_push: {lp.get(\"state\",\"?\")}')
   "
   ```
   If cron sessions appear with `phase=idle` and `updated_at` timestamps
   matching recent session-start events, each one pushed idle.

   **Fix:** Added guard in `run()` → `reserve()` (2 lines):
   ```python
   if payload.get("hook_event_name") == "on_session_start" and state == "idle":
       return None
   ```
   Session tracking still proceeds, but the idle state is not pushed to the
   backend. Requires **Hermes Desktop restart** for plugin to reload
   `star_office_hook.py`.

## Systematic Diagnostic Approach (Layer by Layer)

When sync/state changes don't work, diagnose from outside in:

1. **Star Office layer — does the backend show correct state?**
   ```bash
   curl -s :19000/status | python3 -c "import sys,json; print(json.load(sys.stdin)['state'])"
   ```

2. **Hook layer — did the hook push syncing?**
   ```bash
   python3 -c "import json; d=json.load(open('$TMPDIR/star-office-hermes-hook-state.json'));
   [print(k,v['active']) for k,v in d['sessions'].items() if v['active']];
   print('last_push:', d.get('last_push',{}).get('state'))"
   ```
   If active values are always `executing` never `syncing`, hook detection failed.

3. **Plugin layer — is Hermes passing the right fields?**
   Add diagnostic logging in `_callback()`:
   ```python
   with open("/path/to/hook-events.log", "a") as lf:
       lf.write(f"{ts} | {event} | tool={kwargs.get('tool_name')} | keys={sorted(kwargs.keys())}\n")
   ```
   Check: does `"args"` appear in keys? Does `args.command` contain "codex"?

4. **Hermes layer — is the plugin loaded at all?**
   ```bash
   hermes --profile dev plugins list | grep star
   ```
   Plugin must show "enabled". Requires `HERMES_DESKTOP=1`.

## Plugin Reloading Rules

- **`star_office_hook.py`** — loaded as subprocess per event, changes take effect immediately
- **Plugin `__init__.py`** — loaded by Python import at Hermes startup, requires **full Hermes restart** (not just new conversation)
- **`.pyc` cleanup alone is NOT sufficient** — the in-memory module cache survives
- **`sys.modules.pop()`** in `_load_bridge()` helps but only applies to the bridge, not the plugin module itself

## Debugging Path

1. **Verify Star Office is reachable:**
   ```bash
   curl http://127.0.0.1:19000/health
   curl http://127.0.0.1:19000/status
   ```
   If `/status` returns `state: "idle"` with stale timestamps but the agent is
   active, the hook bridge is not firing.

2. **Check Hermes hooks:**
   ```bash
   hermes --profile dev hooks list
   ```
   If "No shell hooks configured", hooks are not set up. The plugin handles
   hook registration dynamically — this is expected before plugin install.

3. **Check installed plugins:**
   ```bash
   hermes --profile dev plugins list | grep star
   ```
   If `star-office-ui-status` is absent, the plugin was never installed.

4. **Locate the plugin source:**
   The plugin lives in the Star Office repository:
   ```
   ~/HermesWorkspace/Star-Office-UI-Hermes/integrations/hermes/plugin/star-office-ui-status/
   ```
   It contains `__init__.py` (registers hooks) and references
   `../star_office_hook.py` (bridge that sends HTTP to `:19000`).

5. **Install and enable:**
   ```bash
   ln -s ~/HermesWorkspace/Star-Office-UI-Hermes/integrations/hermes/plugin/star-office-ui-status \
         ~/.hermes/profiles/dev/plugins/star-office-ui-status
   hermes --profile dev plugins enable star-office-ui-status
   ```

6. **Restart:** Plugin takes effect on next Hermes session. The current
   conversation (started before install) will not push data. Start a new
   conversation to verify.

## Key Files

| File | Purpose |
|------|---------|
| `integrations/hermes/plugin/star-office-ui-status/__init__.py` | Registers hooks in Hermes |
| `integrations/hermes/star_office_hook.py` | Bridge: receives hook callbacks, HTTP-POSTs to `:19000` |
| `agents-state.json` (Star Office root) | Current agent state (read by Star Office backend) |
| `backend/app.py` (Star Office root) | Star Office Flask backend, serves `/status` and `/health` |

## Build-Time Integration

The `electron/service-manager.mjs` spawns Star Office as a child process:
- Python path: `$STAR_OFFICE_ROOT/.venv/bin/python` (defaults to
  `~/HermesWorkspace/Star-Office-UI-Hermes/.venv/bin/python`)
- Health check: `GET http://127.0.0.1:19000/health`
- Port: 19000

The dashboard's `/api/star-office` endpoint proxies Star Office's `/status`
response for the frontend.

## Important Distinction

`gateway.active_agents` in `gateway_state.json` counts spawned sub-agents
(Codex CLI, Claude Code), NOT the desktop main conversation. A desktop
session running without sub-agents will show `active_agents: 0` even during
active LLM chat. The Star Office plugin uses its own hook-based activity
tracking independent of this field.
