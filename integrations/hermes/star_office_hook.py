#!/usr/bin/env python3
"""Fail-open Hermes shell-hook adapter for Star Office UI (stdlib only)."""

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised by import simulation
    fcntl = None
import json
import os
import re
import shlex
import sys
import sqlite3
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

MAX_DETAIL_LENGTH = 500
MAX_DELIVERY_ATTEMPTS = 2
DEFAULT_URL = "http://127.0.0.1:19000"
DEFAULT_SESSION_STALE_SECONDS = 300

RESEARCH_WORDS = ("search", "browser", "web", "fetch", "read", "lookup", "query")
EXECUTE_WORDS = ("terminal", "shell", "exec", "process", "command", "write", "edit", "patch", "build", "test", "compile", "file")
SYNC_WORDS = ("delegate", "subagent", "agent", "sync", "slack", "github", "drive", "notion", "calendar", "email", "teams")
PRIORITY = {"idle": 0, "writing": 1, "researching": 2, "executing": 3, "syncing": 4, "error": 5}
DETAILS = {
    "idle": "Hermes is ready",
    "writing": "Hermes is composing a response",
    "researching": "Hermes is researching",
    "executing": "Hermes is running a tool",
    "syncing": "Hermes is coordinating work",
    "error": "Hermes encountered a tool error",
}
ACTIVITY_LABELS = {
    "idle": "Idle",
    "llm": "Reasoning",
    "terminal": "Terminal activity",
    "files": "File activity",
    "web": "Web activity",
    "delegation": "Delegating work",
    "other": "Other activity",
}
TOOL_ACTIVITY_NAMES = {
    "terminal": "terminal", "shell": "terminal", "exec": "terminal",
    "exec_command": "terminal", "write_stdin": "terminal", "bash": "terminal",
    "functions.exec_command": "terminal", "functions.write_stdin": "terminal",
    "apply_patch": "files", "read_file": "files", "write_file": "files",
    "find": "files", "glob": "files", "grep": "files", "rg": "files",
    "functions.apply_patch": "files",
    "browser": "web", "web": "web", "web_search": "web", "search_query": "web",
    "fetch": "web", "urlopen": "web", "web.run": "web",
    "spawn_agent": "delegation", "subagent": "delegation", "delegate": "delegation",
    "collaboration.spawn_agent": "delegation",
    "llm": "llm",
}
SYNC_CLI_NAMES = frozenset({"codex", "claude"})
SHELL_NAMES = frozenset({"bash", "dash", "fish", "ksh", "sh", "zsh"})
COMMAND_WRAPPERS = frozenset({"command", "exec", "nohup", "time"})
SUDO_OPTIONS_WITH_VALUE = frozenset({
    "-C", "--close-from", "-D", "--chdir", "-g", "--group",
    "-h", "--host", "-p", "--prompt", "-R", "--chroot",
    "-T", "--command-timeout", "-u", "--user",
})
ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
BACKGROUND_REVIEW_MARKER = (
    "You can only call memory and skill management tools. "
    "Other tools will be denied at runtime"
)


def diagnostic(message):
    print("star-office-hook: " + message, file=sys.stderr)


def state_path():
    return os.getenv("STAR_OFFICE_HOOK_STATE_FILE") or os.path.join(tempfile.gettempdir(), "star-office-hermes-hook-state.json")


def utc_timestamp():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def safe_tool_activity(name):
    value = str(name or "").strip().lower()
    kind = TOOL_ACTIVITY_NAMES.get(value)
    if kind is None:
        for safe_name, category in TOOL_ACTIVITY_NAMES.items():
            if value.startswith(safe_name + "_") or value.startswith(safe_name + "."):
                kind = category
                break
    kind = kind or "other"
    return kind, ACTIVITY_LABELS[kind]


def project_label(current_session_id):
    if not isinstance(current_session_id, str) or not current_session_id or "/" in current_session_id or "\\" in current_session_id:
        return "Hermes session"
    home = os.getenv("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")
    database = Path(home) / "state.db"
    row = None
    try:
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
            for query in (
                    "SELECT cwd, git_repo_root FROM sessions WHERE id = ?",
                    "SELECT cwd, git_repo_root FROM sessions WHERE session_id = ?"):
                try:
                    row = connection.execute(
                        query,
                        (current_session_id,),
                    ).fetchone()
                    if row is not None:
                        break
                except sqlite3.Error:
                    continue
    except (OSError, sqlite3.Error):
        row = None
    for workspace in (row or ()):
        if not isinstance(workspace, str) or not workspace:
            continue
        label = Path(workspace.rstrip("/\\")).name
        if (label and label not in (".", "..") and len(label) <= 64 and
                "/" not in label and "\\" not in label and
                all(ord(character) >= 32 and ord(character) != 127 for character in label)):
            return label
    return "Hermes session"


def tool_state(name):
    value = str(name or "").lower()
    if any(word in value for word in SYNC_WORDS):
        return "syncing"
    if any(word in value for word in RESEARCH_WORDS):
        return "researching"
    if any(word in value for word in EXECUTE_WORDS):
        return "executing"
    return "writing"


def command_invokes_sync_cli(command):
    """Recognize codex/claude in executable position without running a shell."""
    if isinstance(command, (list, tuple)):
        tokens = [str(item) for item in command]
    elif isinstance(command, str):
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
            lexer.whitespace_split = True
            lexer.commenters = ""
            tokens = list(lexer)
        except (TypeError, ValueError):
            return False
    else:
        return False

    expect_command = True
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token and all(character in ";&|()" for character in token):
            expect_command = True
            index += 1
            continue
        if not expect_command:
            index += 1
            continue
        if ASSIGNMENT_RE.match(token):
            index += 1
            continue

        executable = os.path.basename(token).lower()
        if executable in SYNC_CLI_NAMES:
            return True
        if executable in COMMAND_WRAPPERS:
            index += 1
            continue
        if executable == "env":
            index += 1
            while index < len(tokens):
                option = tokens[index]
                if ASSIGNMENT_RE.match(option):
                    index += 1
                elif option in ("-u", "--unset") and index + 1 < len(tokens):
                    index += 2
                elif option.startswith("-"):
                    index += 1
                else:
                    break
            continue
        if executable == "sudo":
            index += 1
            while index < len(tokens):
                option = tokens[index]
                if option == "--":
                    index += 1
                    break
                if option in SUDO_OPTIONS_WITH_VALUE and index + 1 < len(tokens):
                    index += 2
                elif option.startswith("-"):
                    index += 1
                else:
                    break
            continue
        if executable in SHELL_NAMES:
            # Handle the common `sh -c 'codex ...'` form recursively.
            for option_index in range(index + 1, min(len(tokens), index + 4)):
                if tokens[option_index] in ("-c", "-lc", "-cl") and option_index + 1 < len(tokens):
                    return command_invokes_sync_cli(tokens[option_index + 1])
            expect_command = False
            index += 1
            continue

        expect_command = False
        index += 1
    return False


def sync_cli_command(payload):
    """Read only the documented command field, including Hermes' extra envelope."""
    extra = payload.get("extra")
    for source in (payload, extra):
        if not isinstance(source, dict):
            continue
        tool_input = source.get("tool_input")
        if isinstance(tool_input, dict) and "command" in tool_input:
            return tool_input.get("command")
    return None


def explicit_child_id(payload):
    for key in ("child_session_id", "child_subagent_id"):
        if payload.get(key) is not None:
            return str(payload[key])
    extra = payload.get("extra")
    if isinstance(extra, dict):
        for key in ("child_session_id", "child_subagent_id"):
            if extra.get(key) is not None:
                return str(extra[key])
    return None


def explicit_correlation_id(payload):
    """Return only an ID supplied by Hermes, never a synthesized fallback."""
    for source in (payload, payload.get("extra")):
        if isinstance(source, dict):
            for key in ("tool_call_id", "call_id", "correlation_id", "id"):
                if source.get(key) is not None:
                    return str(source[key])
    return None


def normalized_tool_name(payload):
    extra = payload.get("extra")
    extra = extra if isinstance(extra, dict) else {}
    return safe_tool_activity(payload.get("tool_name") or extra.get("tool_name"))[0]


def has_error(payload):
    if payload.get("error") or payload.get("is_error") is True or payload.get("success") is False:
        return True
    candidates = [payload.get("status")]
    extra = payload.get("extra")
    if isinstance(extra, dict):
        if extra.get("error") or extra.get("is_error") is True or extra.get("success") is False:
            return True
        candidates += [extra.get("status")]
    result = payload.get("result")
    if result is None and isinstance(extra, dict):
        result = extra.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (TypeError, ValueError):
            result = None
    if isinstance(result, dict):
        if result.get("error") or result.get("is_error") is True or result.get("success") is False:
            return True
        exit_code = result.get("exit_code")
        if isinstance(exit_code, (int, float)) and not isinstance(exit_code, bool) and exit_code != 0:
            return True
        candidates += [result.get("status")]
    for value in candidates:
        if isinstance(value, str) and value.lower() in ("error", "failed", "failure"):
            return True
    return False


def session_id(payload):
    extra = payload.get("extra")
    extra = extra if isinstance(extra, dict) else {}
    return str(payload.get("session_id") or payload.get("parent_session_id") or
               extra.get("session_id") or extra.get("parent_session_id") or "default")


def turn_id(payload):
    extra = payload.get("extra")
    extra = extra if isinstance(extra, dict) else {}
    return str(payload.get("turn_id") or extra.get("turn_id") or "")


def is_background_review_start(payload):
    message = payload.get("user_message")
    if message is None and isinstance(payload.get("extra"), dict):
        message = payload["extra"].get("user_message")
    return isinstance(message, str) and BACKGROUND_REVIEW_MARKER in message


def session_stale_seconds():
    try:
        value = float(os.getenv(
            "STAR_OFFICE_HOOK_SESSION_STALE_SECONDS",
            str(DEFAULT_SESSION_STALE_SECONDS),
        ))
    except (TypeError, ValueError):
        value = DEFAULT_SESSION_STALE_SECONDS
    return max(1.0, value)


def display_state(data):
    states = []
    last_active = data.get("last_active")
    now = time.time()
    for current_id in sorted(data["sessions"]):
        session = data["sessions"][current_id]
        updated_at = session.get("updated_at")
        # Legacy entries have no freshness metadata. Only the session that
        # just emitted an event may participate, preventing an old on-disk
        # snapshot from contaminating new work forever.
        if updated_at is None and current_id != last_active:
            continue
        if isinstance(updated_at, (int, float)) and now - updated_at > session_stale_seconds():
            continue
        states.append(session.get("phase", "idle"))
        states.extend(session.get("active", {}).values())
        states.extend("syncing" for _ in session.get("subagents", []))
    return max(states or ["idle"], key=lambda item: PRIORITY.get(item, 0))


def apply_event(data, payload):
    event = payload.get("hook_event_name")
    current_session_id = session_id(payload)
    sessions = data.setdefault("sessions", {})
    data["last_active"] = current_session_id
    if event == "on_session_start":
        # A session ID can be reused after an unclean shutdown.  Replace the
        # whole entry so stale tools, subagents, and fallback counters cannot
        # leak into the new session.
        sessions[current_session_id] = {"phase": "idle", "active": {}, "subagents": []}
    session = sessions.setdefault(current_session_id, {"phase": "idle", "active": {}, "subagents": []})
    session["updated_at"] = time.time()
    active = session.setdefault("active", {})
    subagents = session.setdefault("subagents", [])
    fallback_tools = session.setdefault("fallback_tools", {})
    safe_active = session.setdefault("safe_active", {})
    background_reviews = session.setdefault("background_reviews", [])
    extra = payload.get("extra")
    extra = extra if isinstance(extra, dict) else {}

    if event == "on_session_start":
        active.clear()
        safe_active.clear()
        subagents.clear()
        fallback_tools.clear()
        background_reviews.clear()
    elif event == "pre_llm_call":
        current_turn_id = turn_id(payload)
        if is_background_review_start(payload):
            if current_turn_id and current_turn_id not in background_reviews:
                background_reviews.append(current_turn_id)
            session["phase"] = "syncing"
        else:
            session["phase"] = "writing"
    elif event == "pre_tool_call":
        call_id = explicit_correlation_id(payload)
        if call_id is None:
            counter = int(session.get("fallback_counter", 0)) + 1
            session["fallback_counter"] = counter
            call_id = "fallback:%d" % counter
            fallback_tools.setdefault(normalized_tool_name(payload), []).append(call_id)
        active[call_id] = (
            "syncing" if background_reviews or command_invokes_sync_cli(sync_cli_command(payload))
            else tool_state(payload.get("tool_name") or extra.get("tool_name"))
        )
        safe_active[call_id] = (
            "delegation" if background_reviews
            else safe_tool_activity(payload.get("tool_name") or extra.get("tool_name"))[0]
        )
        session["phase"] = "syncing" if background_reviews else "writing"
    elif event == "post_tool_call":
        call_id = explicit_correlation_id(payload)
        if call_id is not None:
            active.pop(call_id, None)
            safe_active.pop(call_id, None)
        else:
            queue = fallback_tools.get(normalized_tool_name(payload), [])
            if queue:
                finished_id = queue.pop(0)
                active.pop(finished_id, None)
                safe_active.pop(finished_id, None)
                if not queue:
                    fallback_tools.pop(normalized_tool_name(payload), None)
        session["phase"] = (
            "error" if has_error(payload)
            else "syncing" if background_reviews
            else "writing" if active or subagents
            else "idle"
        )
    elif event == "subagent_start":
        child_id = explicit_child_id(payload)
        if child_id is None:
            counter = int(session.get("fallback_subagent_counter", 0)) + 1
            session["fallback_subagent_counter"] = counter
            child_id = "fallback-subagent:%d" % counter
        if child_id not in subagents:
            subagents.append(child_id)
        session["phase"] = "writing"
    elif event == "subagent_stop":
        child_id = explicit_child_id(payload)
        if child_id is not None and str(child_id) in subagents:
            subagents.remove(str(child_id))
        elif child_id is not None:
            diagnostic("ignoring stop for an untracked subagent")
        elif child_id is None and subagents:
            subagents.pop(0)
        if child_id is not None and str(child_id) != current_session_id:
            # Tool hooks emitted by a child may have created a session entry
            # under the child's own ID.  Its lifecycle is owned by this stop
            # event, so discard that orphan along with the parent's tracking.
            sessions.pop(str(child_id), None)
        child_status = payload.get("child_status") or extra.get("child_status")
        failed = isinstance(child_status, str) and child_status.lower() in ("failed", "error", "interrupted")
        session["phase"] = "error" if failed or has_error(payload) else "writing"
    elif event in ("post_llm_call", "on_session_end"):
        current_turn_id = turn_id(payload)
        if current_turn_id in background_reviews:
            background_reviews.remove(current_turn_id)
        active.clear()
        safe_active.clear()
        subagents.clear()
        fallback_tools.clear()
        session["phase"] = "syncing" if background_reviews else "idle"
    elif event in ("on_session_finalize", "on_session_reset"):
        sessions.pop(current_session_id, None)
    else:
        raise ValueError("unsupported event")
    state = display_state(data)
    if event in ("post_llm_call", "on_session_end", "on_session_finalize", "on_session_reset"):
        activity_kind = "idle" if state == "idle" else "llm"
    elif event in ("on_session_start", "pre_llm_call"):
        activity_kind = "llm"
    elif event in ("subagent_start", "subagent_stop"):
        activity_kind = "delegation" if any(item.get("subagents") for item in sessions.values()) else "llm"
    elif event == "pre_tool_call":
        activity_kind = safe_tool_activity(payload.get("tool_name") or extra.get("tool_name"))[0]
    else:
        remaining_kinds = [
            kind for item in sessions.values()
            for kind in item.get("safe_active", {}).values()
            if kind in ACTIVITY_LABELS
        ]
        activity_kind = remaining_kinds[-1] if remaining_kinds else ("other" if has_error(payload) else "llm")
    now = utc_timestamp()
    old_activity = data.get("activity") if isinstance(data.get("activity"), dict) else {}
    events = old_activity.get("recentEvents") if isinstance(old_activity.get("recentEvents"), list) else []
    if old_activity.get("activityKind") != activity_kind:
        events = (events + [{
            "kind": activity_kind,
            "label": ACTIVITY_LABELS[activity_kind],
            "at": now,
        }])[-6:]
        started_at = now
    else:
        started_at = old_activity.get("startedAt") or now
    data["activity"] = {
        "projectLabel": project_label(current_session_id),
        "activityKind": activity_kind,
        "activityLabel": ACTIVITY_LABELS[activity_kind],
        "activeSubagents": min(99, sum(len(item.get("subagents", [])) for item in sessions.values())),
        "startedAt": started_at,
        "updatedAt": now,
        "recentEvents": events[-6:],
    }
    return state


def push(state, detail, activity=None):
    base = (os.getenv("STAR_OFFICE_URL") or DEFAULT_URL).rstrip("/")
    body_data = {"state": state, "detail": detail[:MAX_DETAIL_LENGTH]}
    if activity is not None:
        body_data["activity"] = activity
    body = json.dumps(body_data).encode()
    headers = {"Content-Type": "application/json"}
    token = os.getenv("STAR_OFFICE_API_TOKEN", "")
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(base + "/set_state", data=body, headers=headers, method="POST")
    try:
        timeout = float(os.getenv("STAR_OFFICE_HOOK_TIMEOUT", "0.75"))
    except (TypeError, ValueError):
        timeout = 0.75
    with urllib.request.urlopen(request, timeout=max(0.05, min(timeout, 10.0))) as response:
        if response.status < 200 or response.status >= 300:
            raise OSError("HTTP %s" % response.status)


def locked_data(path, transition):
    if fcntl is None:
        raise RuntimeError("cross-process locking is unsupported on this platform (macOS/Linux required)")
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    lock_path = path + ".lock"
    with open(lock_path, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(path, "r", encoding="utf-8") as source:
                data = json.load(source)
                if not isinstance(data, dict):
                    raise ValueError("invalid state data")
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            data = {"sessions": {}, "last_push": None}
        result = transition(data)
        fd, temporary = tempfile.mkstemp(prefix=".hook-state-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(data, output, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return result


def run(payload):
    path = state_path()

    def reserve(data):
        state = apply_event(data, payload)
        update = {"state": state, "detail": DETAILS[state], "activity": data["activity"]}
        desired = data.get("desired")
        if not isinstance(desired, dict) or desired.get("update") != update:
            sequence = int(data.get("sequence", 0)) + 1
            data["sequence"] = sequence
            desired = {"sequence": sequence, "update": update}
            data["desired"] = desired
        if data.get("last_push") == update:
            return None
        return desired["sequence"], desired["update"]

    reservation = locked_data(path, reserve)
    if reservation is None:
        return
    sequence, update = reservation
    for _ in range(MAX_DELIVERY_ATTEMPTS):
        try:
            push(update["state"], update["detail"], update["activity"])
        except Exception as exc:
            diagnostic("activity delivery failed")
            return

        def delivered(data):
            desired = data.get("desired", {})
            if desired.get("sequence") == sequence and desired.get("update") == update:
                data["last_push"] = update
                return None
            # This obsolete request just became the backend's last completed
            # request. Any prior confirmation of the desired update is no
            # longer authoritative, so leave it retryable and correct it now.
            data["last_push"] = None
            if isinstance(desired, dict) and "sequence" in desired and "update" in desired:
                return desired["sequence"], desired["update"]
            return None

        correction = locked_data(path, delivered)
        if correction is None:
            return
        sequence, update = correction


def main():
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        run(payload)
    except Exception as exc:
        diagnostic("unsupported platform" if fcntl is None else "hook event ignored")
    return 0


if __name__ == "__main__":
    sys.exit(main())
