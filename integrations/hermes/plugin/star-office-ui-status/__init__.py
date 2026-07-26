"""Observer-only Hermes plugin for the Star Office UI status bridge."""

import importlib.util
import os
from pathlib import Path
import sys

HOOK_EVENTS = (
    "pre_llm_call",
    "post_llm_call",
    "pre_tool_call",
    "post_tool_call",
    "on_session_start",
    "on_session_end",
    "on_session_finalize",
    "on_session_reset",
    "subagent_start",
    "subagent_stop",
)

_TOP_LEVEL_FIELDS = ("tool_name", "session_id", "turn_id", "user_message", "args")
_EXTRA_FIELDS = (
    "parent_session_id",
    "task_id",
    "tool_call_id",
    "call_id",
    "correlation_id",
    "id",
    "child_session_id",
    "child_subagent_id",
    "child_status",
    "status",
    "success",
    "is_error",
    "error",
    "result",
)
_bridge = None


def _diagnostic():
    print("star-office-plugin: status observer failed", file=sys.stderr)


def _bridge_path():
    """Resolve through an installed symlink back to the repository bridge."""
    return Path(__file__).resolve().parents[2] / "star_office_hook.py"


def _load_bridge():
    global _bridge
    if _bridge is not None:
        return _bridge
    path = _bridge_path()
    spec = importlib.util.spec_from_file_location("star_office_ui_status_bridge", path)
    if spec is None or spec.loader is None:
        raise ImportError("bridge module is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _bridge = module
    return _bridge


def _payload(event, kwargs):
    payload = {"hook_event_name": event}
    nested = kwargs.get("extra")
    nested = nested if isinstance(nested, dict) else {}
    for field in _TOP_LEVEL_FIELDS:
        if field in kwargs:
            # Hermes passes tool arguments as "args", hook expects "tool_input"
            key = "tool_input" if field == "args" else field
            payload[key] = kwargs[field]
        elif field in nested:
            key = "tool_input" if field == "args" else field
            payload[key] = nested[field]
    extra = {}
    for field in _EXTRA_FIELDS:
        if field in kwargs:
            extra[field] = kwargs[field]
        elif field in nested:
            extra[field] = nested[field]
    payload["extra"] = extra
    return payload


def _callback(event):
    def observe(**kwargs):
        try:
            bridge = _load_bridge()
            if bridge is None:
                raise RuntimeError("bridge module failed to load")
            bridge.run(_payload(event, kwargs))
        except Exception:
            _diagnostic()
        return None

    observe.__name__ = "observe_" + event
    return observe


def register(ctx):
    """Register lifecycle observers without adding an LLM-visible tool."""
    if os.environ.get("HERMES_DESKTOP") != "1":
        return
    for event in HOOK_EVENTS:
        ctx.register_hook(event, _callback(event))
