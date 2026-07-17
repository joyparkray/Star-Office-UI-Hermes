#!/usr/bin/env python3
"""Fail-open Hermes shell-hook adapter for Star Office UI (stdlib only)."""

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised by import simulation
    fcntl = None
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request

MAX_DETAIL_LENGTH = 500
MAX_DELIVERY_ATTEMPTS = 2
DEFAULT_URL = "http://127.0.0.1:19000"

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


def diagnostic(message):
    print("star-office-hook: " + message, file=sys.stderr)


def state_path():
    return os.getenv("STAR_OFFICE_HOOK_STATE_FILE") or os.path.join(tempfile.gettempdir(), "star-office-hermes-hook-state.json")


def tool_state(name):
    value = str(name or "").lower()
    if any(word in value for word in SYNC_WORDS):
        return "syncing"
    if any(word in value for word in RESEARCH_WORDS):
        return "researching"
    if any(word in value for word in EXECUTE_WORDS):
        return "executing"
    return "writing"


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
    return str(payload.get("tool_name") or extra.get("tool_name") or "tool").strip().lower()


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


def display_state(data):
    states = []
    for session_id in sorted(data["sessions"]):
        session = data["sessions"][session_id]
        states.append(session.get("phase", "idle"))
        states.extend(session.get("active", {}).values())
        states.extend("syncing" for _ in session.get("subagents", []))
    return max(states or ["idle"], key=lambda item: PRIORITY.get(item, 0))


def apply_event(data, payload):
    event = payload.get("hook_event_name")
    current_session_id = session_id(payload)
    sessions = data.setdefault("sessions", {})
    session = sessions.setdefault(current_session_id, {"phase": "idle", "active": {}, "subagents": []})
    active = session.setdefault("active", {})
    subagents = session.setdefault("subagents", [])
    fallback_tools = session.setdefault("fallback_tools", {})
    extra = payload.get("extra")
    extra = extra if isinstance(extra, dict) else {}

    if event in ("on_session_start", "pre_llm_call"):
        session["phase"] = "writing"
        if event == "on_session_start":
            active.clear()
            subagents.clear()
            fallback_tools.clear()
    elif event == "pre_tool_call":
        call_id = explicit_correlation_id(payload)
        if call_id is None:
            counter = int(session.get("fallback_counter", 0)) + 1
            session["fallback_counter"] = counter
            call_id = "fallback:%d" % counter
            fallback_tools.setdefault(normalized_tool_name(payload), []).append(call_id)
        active[call_id] = tool_state(payload.get("tool_name") or extra.get("tool_name"))
        session["phase"] = "writing"
    elif event == "post_tool_call":
        call_id = explicit_correlation_id(payload)
        if call_id is not None:
            active.pop(call_id, None)
        else:
            queue = fallback_tools.get(normalized_tool_name(payload), [])
            if queue:
                active.pop(queue.pop(0), None)
                if not queue:
                    fallback_tools.pop(normalized_tool_name(payload), None)
        session["phase"] = "error" if has_error(payload) else "writing"
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
        child_status = payload.get("child_status") or extra.get("child_status")
        failed = isinstance(child_status, str) and child_status.lower() in ("failed", "error", "interrupted")
        session["phase"] = "error" if failed or has_error(payload) else "writing"
    elif event in ("post_llm_call", "on_session_end"):
        active.clear()
        subagents.clear()
        fallback_tools.clear()
        session["phase"] = "idle"
    elif event in ("on_session_finalize", "on_session_reset"):
        sessions.pop(current_session_id, None)
    else:
        raise ValueError("unsupported event")
    return display_state(data)


def push(state, detail):
    base = (os.getenv("STAR_OFFICE_URL") or DEFAULT_URL).rstrip("/")
    body = json.dumps({"state": state, "detail": detail[:MAX_DETAIL_LENGTH]}).encode()
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
        update = {"state": state, "detail": DETAILS[state]}
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
            push(update["state"], update["detail"])
        except Exception as exc:
            diagnostic(str(exc)[:160])
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
        diagnostic(str(exc)[:160])
    return 0


if __name__ == "__main__":
    sys.exit(main())
