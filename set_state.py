#!/usr/bin/env python3
"""Update Star Office UI state (for testing or agent-driven sync).

For automatic state sync from OpenClaw: add a rule in your agent SOUL.md or AGENTS.md:
  Before starting a task: run `python3 set_state.py writing "doing XYZ"`.
  After finishing: run `python3 set_state.py idle "ready"`.
The office UI reads state from the same state.json this script writes.
"""

import json
import os
import sys
import tempfile
from datetime import datetime

STATE_FILE = os.environ.get(
    "STAR_OFFICE_STATE_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json"),
)

VALID_STATES = [
    "idle",
    "writing",
    "receiving",
    "replying",
    "researching",
    "executing",
    "syncing",
    "error"
]
MAX_DETAIL_LENGTH = 500

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "state": "idle",
        "detail": "待命中...",
        "progress": 0,
        "updated_at": datetime.now().isoformat()
    }

def save_state(state):
    directory = os.path.dirname(os.path.abspath(STATE_FILE))
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".star-office-state-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, STATE_FILE)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python set_state.py <state> [detail]")
        print(f"状态选项: {', '.join(VALID_STATES)}")
        print("\n例子:")
        print("  python set_state.py idle")
        print("  python set_state.py researching \"在查 Godot MCP...\"")
        print("  python set_state.py writing \"在写热点日报模板...\"")
        sys.exit(1)
    
    state_name = sys.argv[1]
    detail = sys.argv[2] if len(sys.argv) > 2 else ""
    
    if state_name not in VALID_STATES:
        print(f"无效状态: {state_name}")
        print(f"有效选项: {', '.join(VALID_STATES)}")
        sys.exit(1)
    if len(detail) > MAX_DETAIL_LENGTH:
        print(f"描述过长（最多 {MAX_DETAIL_LENGTH} 个字符）")
        sys.exit(1)
    
    state = load_state()
    state["state"] = state_name
    state["detail"] = detail
    state["updated_at"] = datetime.now().isoformat()
    
    save_state(state)
    print(f"状态已更新: {state_name} - {detail}")
