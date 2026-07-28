#!/usr/bin/env python3
"""验证 star_office_hook.py 的状态机修复是否正确。

用法:
    cd ~/HermesWorkspace/Star-Office-UI-Hermes
    .venv/bin/python scripts/star-office-hook-verify.py
"""

import importlib.util
import os, sys

HOOK_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "integrations", "hermes", "star_office_hook.py"
)

spec = importlib.util.spec_from_file_location("hook", HOOK_PATH)
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)

PASSED = 0
FAILED = 0

def check(name, condition):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ✅ {name}")
    else:
        FAILED += 1
        print(f"  ❌ {name}")

def ev(data, payload):
    return h.apply_event(data, payload)

print("=== Star Office Hook 状态机验证 ===\n")

# 1. on_session_start → idle
d = {"sessions": {}}
ev(d, {"hook_event_name": "on_session_start", "session_id": "s1"})
check("1. on_session_start → idle", h.display_state(d) == "idle")

# 2. pre_llm_call → writing
ev(d, {"hook_event_name": "pre_llm_call", "session_id": "s1"})
check("2. pre_llm_call → writing", h.display_state(d) == "writing")

# 3. pre_tool_call → executing
ev(d, {"hook_event_name": "pre_tool_call", "session_id": "s1", "tool_name": "terminal", "tool_call_id": "t1"})
check("3. pre_tool_call → executing", h.display_state(d) == "executing")

# 4. post_tool_call (最后一个) → idle 🔑
ev(d, {"hook_event_name": "post_tool_call", "session_id": "s1", "tool_name": "terminal", "tool_call_id": "t1"})
check("4. post_tool_call(最后) → idle 🔑", h.display_state(d) == "idle")

# 5. post_llm_call → idle
ev(d, {"hook_event_name": "pre_llm_call", "session_id": "s1"})
ev(d, {"hook_event_name": "post_llm_call", "session_id": "s1"})
check("5. post_llm_call → idle", h.display_state(d) == "idle")

# 6. 并发工具
ev(d, {"hook_event_name": "pre_tool_call", "session_id": "s1", "tool_name": "shell", "tool_call_id": "a"})
ev(d, {"hook_event_name": "pre_tool_call", "session_id": "s1", "tool_name": "shell", "tool_call_id": "b"})
check("6a. 2个工具并发 → executing", h.display_state(d) == "executing")
ev(d, {"hook_event_name": "post_tool_call", "session_id": "s1", "tool_name": "shell", "tool_call_id": "a"})
check("6b. 完成1个 → executing", h.display_state(d) == "executing")
ev(d, {"hook_event_name": "post_tool_call", "session_id": "s1", "tool_name": "shell", "tool_call_id": "b"})
check("6c. 完成全部 → idle 🔑", h.display_state(d) == "idle")

# 7. 工具错误
ev(d, {"hook_event_name": "pre_tool_call", "session_id": "s1", "tool_name": "shell", "tool_call_id": "err"})
ev(d, {"hook_event_name": "post_tool_call", "session_id": "s1", "tool_name": "shell", "tool_call_id": "err", "error": "fail"})
check("7. 工具错误 → error", h.display_state(d) == "error")

# 8. 子代理孤儿清理 🔑
d2 = {"sessions": {}}
ev(d2, {"hook_event_name": "subagent_start", "session_id": "parent", "child_session_id": "child-x"})
check("8a. 子代理启动 → syncing", h.display_state(d2) == "syncing")
ev(d2, {"hook_event_name": "pre_tool_call", "session_id": "child-x", "tool_name": "shell", "tool_call_id": "cx"})
check("8b. 子代理工具 → syncing (P4 > P3)", h.display_state(d2) == "syncing")
ev(d2, {"hook_event_name": "subagent_stop", "session_id": "parent", "child_session_id": "child-x"})
check("8c. stop 后孤儿已清理 🔑", "child-x" not in d2["sessions"])

# 9. on_session_start 覆盖残留 🔑
d3 = {"sessions": {"reused": {"phase": "executing", "active": {"old": "executing"}, "subagents": ["ghost"]}}}
ev(d3, {"hook_event_name": "on_session_start", "session_id": "reused"})
check("9. 重启覆盖残留 → idle 🔑", h.display_state(d3) == "idle")

# 10. codex/claude sync 检测
d4 = {"sessions": {}}
ev(d4, {"hook_event_name": "pre_tool_call", "session_id": "s1", "tool_call_id": "s1",
         "tool_name": "terminal", "tool_input": {"command": "codex exec task"}})
check("10a. codex 命令 → syncing", h.display_state(d4) == "syncing")

d5 = {"sessions": {}}
ev(d5, {"hook_event_name": "pre_tool_call", "session_id": "s1", "tool_call_id": "s2",
         "tool_name": "terminal", "tool_input": {"command": "claude -p review"}})
check("10b. claude 命令 → syncing", h.display_state(d5) == "syncing")

d6 = {"sessions": {}}
ev(d6, {"hook_event_name": "pre_tool_call", "session_id": "s1", "tool_call_id": "s3",
         "tool_name": "terminal", "tool_input": {"command": "npm test"}})
check("10c. 普通命令 → executing", h.display_state(d6) == "executing")

d7 = {"sessions": {}}
ev(d7, {"hook_event_name": "pre_tool_call", "session_id": "s1", "tool_call_id": "s4",
         "tool_name": "terminal"})
check("10d. 无 tool_input → executing", h.display_state(d7) == "executing")

print(f"\n{'='*30}")
print(f"  {PASSED} 通过 / {PASSED + FAILED} 总计")
print(f"{'='*30}")

sys.exit(0 if FAILED == 0 else 1)
