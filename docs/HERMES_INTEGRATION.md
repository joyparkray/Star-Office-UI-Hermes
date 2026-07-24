# Hermes integration

This integration mirrors Hermes activity into Star Office UI without modifying Hermes core. Its schema and parser compatibility were verified against Hermes v0.18.2. See the official [Hermes Event Hooks documentation](https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks). It requires Python 3 and a running Star Office backend.

Hermes v0.18.2 registers configured shell hooks on CLI and Gateway startup, but Hermes Desktop sessions use `tui_gateway`, which does not register those shell hooks. Desktop therefore requires the enabled `star-office-ui-status` Python plugin included in this repository. Desktop support should not be considered active until that plugin is installed, enabled, restarted, and verified. CLI/Gateway users may retain the shell-hook configuration below.

## Setup

1. Start Star Office UI. For local-only access, set `STAR_BACKEND_HOST=127.0.0.1`; the compatibility default when the variable is unset is `0.0.0.0`. An explicitly configured invalid host fails safe to loopback (`127.0.0.1`) with a warning.
2. Set the same strong random `STAR_OFFICE_API_TOKEN` for both the backend and Hermes hook environment whenever binding outside loopback or exposing the service through a public tunnel; it is strongly required in those configurations. When configured, `/set_state` rejects missing or invalid bearer credentials. Avoid putting tokens in command lines or committed config.
3. Copy `integrations/hermes/hooks.example.yaml` into the appropriate hooks section of your Hermes configuration and replace the placeholder with the repository's absolute path. The exact configuration file location depends on your Hermes installation.
4. Restart both the backend (after environment changes) and Hermes (after hook configuration changes).
5. Verify with `hermes hooks list`, `hermes hooks test pre_tool_call`, `hermes hooks test post_tool_call`, `hermes hooks test subagent_start`, and `hermes hooks test subagent_stop`. Diagnose configuration with `hermes hooks doctor`. On first use Hermes may request consent or require the command to be added to its hook allowlist; review the absolute command and approve it through Hermes rather than disabling that safeguard.

## Hermes Desktop plugin

From the repository root, install the repository-contained plugin with a symlink (recommended for local development):

```bash
mkdir -p ~/.hermes/plugins
ln -s "$PWD/integrations/hermes/plugin/star-office-ui-status" ~/.hermes/plugins/star-office-ui-status
hermes plugins enable star-office-ui-status
```

If symlinks are unavailable, copy the plugin directory and the shared bridge at the relative location the adapter expects:

```bash
mkdir -p ~/.hermes/plugins/star-office-ui-status
cp integrations/hermes/plugin/star-office-ui-status/{__init__.py,plugin.yaml} ~/.hermes/plugins/star-office-ui-status/
cp integrations/hermes/star_office_hook.py ~/.hermes/star_office_hook.py
hermes plugins enable star-office-ui-status
```

Restart Hermes Desktop after enabling it. Confirm discovery with `hermes plugins list`, then verify with a real Star Office watcher/E2E run that LLM, tool, failure, and completion events change status. `hermes hooks test` verifies shell hooks, not the Desktop plugin runtime.

The plugin is intentionally gated to the real Desktop backend process (`HERMES_DESKTOP=1`) and registers observers only there; it adds no Hermes-visible tool. CLI/Gateway continue to use the configured shell hooks as their sole delivery channel. Its callback adapter discards prompts, messages, tool arguments, commands, child goals/summaries, conversation history, and unrelated fields before invoking the existing bridge. A tool result is passed in memory only for conservative error detection; the bridge persists and displays aggregate state with fixed generic details, never the raw result.

Hermes `PluginManager.invoke_hook` invokes plugin callbacks with keyword arguments, matching this adapter's callback contract. Installation and restart are still followed by the real end-to-end Desktop validation above; unit tests and `hermes hooks test` do not replace it.

Environment variables:

| Variable | Purpose | Default |
|---|---|---|
| `STAR_OFFICE_URL` | Backend base URL | `http://127.0.0.1:19000` |
| `STAR_OFFICE_API_TOKEN` | Optional shared bearer token | unset |
| `STAR_OFFICE_HOOK_TIMEOUT` | HTTP timeout in seconds (clamped to 0.05–10) | `0.75` |
| `STAR_OFFICE_HOOK_STATE_FILE` | Hook coordination file | OS temporary directory |
| `STAR_BACKEND_HOST` | Backend bind address | `0.0.0.0` |

Backend HTTP `/set_state` and the Hermes adapter accept only the six canonical states. The direct `set_state.py` file helper retains the desktop-pet-compatible `receiving` and `replying` animations as well, for eight helper states total; it does not map those animations away. Optional `detail` is limited to 500 Unicode characters and is rejected if longer.

## Status and concurrency policy

LLM work maps to `writing`; read-only search/browser/web/fetch tools to `researching`; shell, edit, build, and test tools to `executing`; delegation and cross-system tools to `syncing`; tool failures to `error`; completed turns and sessions to `idle`. Details are fixed generic phrases: tool inputs, commands, messages, paths, results, and tokens are never forwarded.

The adapter tracks correlated active calls and concurrent subagents per session under a cross-process file lock. Subagent events prefer `child_session_id` for correlation, falling back to `child_subagent_id` when no child session ID is supplied. ID-less tool calls use a per-session, per-tool FIFO with unique counters. A subagent stop without a child ID removes one tracked child deterministically; an unknown supplied ID removes nothing. Finishing one call does not hide another. Across concurrent sessions, the displayed state is deterministic: `error`, `syncing`, `executing`, `researching`, `writing`, then `idle`; ties are resolved from sorted session/call data. Reset/finalize removes that session. Successful lifecycle completion clears active calls. Identical state/detail updates are edge-triggered and not resent.

The hook is fail-open: malformed payloads, lock/state-file trouble, authentication errors, and an unavailable backend produce a short stderr diagnostic and exit 0, so Hermes work continues. Cross-process locking is supported on macOS and Linux. The file lock is released before HTTP delivery, and each hook's network wait is bounded by `STAR_OFFICE_HOOK_TIMEOUT`; one unavailable-backend request therefore does not make other hook processes wait on its timeout. A failed push is not recorded as delivered and will be retried by a later event. `MAX_DELIVERY_ATTEMPTS=2` corrects one observed out-of-order race; under continuous deeper races, the next hook event retries the desired state. No heartbeat is currently included. Because the coordination file is temporary by default, an OS cleanup or reboot resets edge/concurrency history safely.

Backend `state.json` replacement is atomic and read-modify-write operations are protected within one threaded Flask process. Multi-worker deployments require shared-filesystem and process-level coordination beyond this in-process lock.

## Troubleshooting and rollback

- Run `hermes hooks doctor`, then `hermes hooks list`; confirm every event in the example is registered.
- Run the script manually with a small JSON event on stdin and check stderr. Confirm the URL/port and that backend and hook tokens match.
- If the UI stays busy after an interrupted lifecycle, send a session reset/finalize event or remove the hook state file while no hooks are running.
- For Desktop rollback, run `hermes plugins disable star-office-ui-status`, remove `~/.hermes/plugins/star-office-ui-status`, remove `~/.hermes/star_office_hook.py` if it was created by the copy installation, and restart Desktop. Shell hooks can remain enabled for CLI/Gateway.
- To remove CLI/Gateway integration too, remove the ten Star Office shell-hook registrations, restart Hermes, and optionally delete the temporary state and `.lock` files. The backend remains usable manually or by OpenClaw.

Codex or Claude can optionally appear as guest agents using the existing `/join-agent` and `/agent-push` APIs. Provision join keys through the existing project workflow; never hard-code or commit them.
