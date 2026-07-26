import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import shlex
import sqlite3
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(__file__))
HOOK_PATH = os.path.join(ROOT, "integrations", "hermes", "star_office_hook.py")
SPEC = importlib.util.spec_from_file_location("star_office_hook", HOOK_PATH)
hook = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hook)


class Response:
    status = 200
    def __enter__(self): return self
    def __exit__(self, *args): return False


class HookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "hook.json")
        self.env = mock.patch.dict(os.environ, {"STAR_OFFICE_HOOK_STATE_FILE": self.path}, clear=False)
        self.env.start()
        self.pushes = []
        self.push_patch = mock.patch.object(hook, "push", side_effect=lambda state, detail, activity=None: self.pushes.append((state, detail)))
        self.push_patch.start()

    def tearDown(self):
        self.push_patch.stop()
        self.env.stop()
        self.temp.cleanup()

    def event(self, name, **values):
        payload = {"hook_event_name": name, "session_id": "s1"}
        payload.update(values)
        hook.run(payload)

    def test_mapping_and_redacted_generic_details(self):
        cases = [("web_search", "researching"), ("terminal_exec", "executing"), ("delegate_agent", "syncing"), ("mystery", "writing")]
        for index, (tool, expected) in enumerate(cases):
            self.event("pre_tool_call", tool_name=tool, tool_call_id=str(index), tool_input={"secret": "DO_NOT_LEAK"})
            self.assertEqual(self.pushes[-1][0], expected)
            self.assertNotIn("DO_NOT_LEAK", self.pushes[-1][1])
            self.event("on_session_reset")

    def test_codex_and_claude_commands_use_syncing_until_completion(self):
        commands = (
            "codex exec --full-auto 'review this'",
            "/opt/tools/claude -p 'review this'",
            "cd /tmp && env MODE=review codex exec",
            "sudo -u nobody claude -p review",
            "bash -lc 'claude -p review'",
            ["codex", "exec", "review"],
        )
        for index, command in enumerate(commands):
            with self.subTest(command=command):
                call_id = "sync-cli-%d" % index
                self.event("pre_tool_call", tool_name="terminal", tool_call_id=call_id,
                           tool_input={"command": command})
                self.assertEqual(self.pushes[-1][0], "syncing")
                self.event("post_tool_call", tool_name="terminal", tool_call_id=call_id)
                self.assertEqual(self.pushes[-1][0], "idle")
                self.event("on_session_reset")

    def test_sync_cli_detection_requires_executable_position(self):
        commands = (
            "echo codex",
            "printf '%s' claude",
            "python script.py --provider codex",
            "cat /tmp/claude.log",
            "my-codex-wrapper run",
            "echo codex && true",
        )
        for index, command in enumerate(commands):
            with self.subTest(command=command):
                call_id = "ordinary-cli-%d" % index
                self.event("pre_tool_call", tool_name="terminal", tool_call_id=call_id,
                           tool_input={"command": command})
                self.assertEqual(self.pushes[-1][0], "executing")
                self.event("on_session_reset")

    def test_sync_cli_command_supports_extra_envelope(self):
        self.event("pre_tool_call", extra={
            "tool_name": "terminal",
            "tool_call_id": "extra-command",
            "tool_input": {"command": "command claude --print"},
        })
        self.assertEqual(self.pushes[-1][0], "syncing")

    def test_background_review_stays_syncing_until_its_turn_finishes(self):
        review_message = (
            "Review the conversation.\n\n"
            + hook.BACKGROUND_REVIEW_MARKER
            + " — do not attempt them."
        )
        self.event("pre_llm_call", turn_id="review-turn", user_message=review_message)
        self.assertEqual(self.pushes[-1][0], "syncing")

        # The foreground turn can emit its end hook after the review thread
        # starts. It must not make the review animation disappear.
        self.event("on_session_end", turn_id="foreground-turn")
        self.assertEqual(self.pushes[-1][0], "syncing")

        self.event("pre_tool_call", turn_id="review-turn", tool_name="skills",
                   tool_call_id="review-tool", tool_input={"action": "update"})
        self.assertEqual(self.pushes[-1][0], "syncing")
        self.event("post_tool_call", turn_id="review-turn", tool_name="skills",
                   tool_call_id="review-tool")
        self.assertEqual(self.pushes[-1][0], "syncing")
        self.event("post_llm_call", turn_id="review-turn")
        self.assertEqual(self.pushes[-1][0], "idle")

    def test_fresh_sessions_share_global_priority_but_stale_sessions_do_not(self):
        self.event("pre_tool_call", session_id="old", tool_name="delegate_agent",
                   tool_call_id="old-sync")
        self.event("pre_tool_call", session_id="current", tool_name="shell",
                   tool_call_id="current-exec")
        with open(self.path, encoding="utf-8") as source:
            data = json.load(source)
        self.assertEqual(hook.display_state(data), "syncing")

        data["sessions"]["old"]["updated_at"] = 1
        with mock.patch.object(hook.time, "time", return_value=1000), \
                mock.patch.dict(os.environ, {
                    "STAR_OFFICE_HOOK_SESSION_STALE_SECONDS": "10",
                }, clear=False):
            self.assertEqual(hook.display_state(data), "executing")

    def test_legacy_session_without_timestamp_cannot_pollute_current_session(self):
        data = {
            "sessions": {
                "s1": {"phase": "syncing", "active": {"stale": "syncing"}},
                "s2": {"phase": "idle", "active": {}, "updated_at": hook.time.time()},
            },
            "last_active": "s2",
        }
        self.assertEqual(hook.display_state(data), "idle")

    def test_tool_names_map_only_to_safe_activity_categories(self):
        cases = {
            "terminal_exec": ("terminal", "Terminal activity"),
            "apply_patch": ("files", "File activity"),
            "browser_search": ("web", "Web activity"),
            "spawn_agent": ("delegation", "Delegating work"),
            "mystery_private_tool": ("other", "Other activity"),
        }
        for raw, expected in cases.items():
            self.assertEqual(hook.safe_tool_activity(raw), expected)
        self.assertNotIn("mystery_private_tool", json.dumps(list(cases.values())))

    def test_project_label_uses_only_database_workspace_basename_and_fallback(self):
        hermes_home = os.path.join(self.temp.name, "hermes")
        os.makedirs(hermes_home)
        database = os.path.join(hermes_home, "state.db")
        with sqlite3.connect(database) as connection:
            connection.execute(
                "CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, git_repo_root TEXT, title TEXT)"
            )
            connection.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?)",
                ("safe-session", "/Users/private/VisibleProject", None, "SECRET TITLE"),
            )
        with mock.patch.dict(os.environ, {"HERMES_HOME": hermes_home}):
            self.assertEqual(hook.project_label("safe-session"), "VisibleProject")
            self.assertEqual(hook.project_label("missing"), "Hermes session")
        self.assertEqual(hook.project_label("bad/session"), "Hermes session")

    def test_project_label_supports_fixed_session_id_query_without_other_columns(self):
        hermes_home = os.path.join(self.temp.name, "hermes-session-id")
        os.makedirs(hermes_home)
        database = os.path.join(hermes_home, "state.db")
        with sqlite3.connect(database) as connection:
            connection.execute(
                "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, cwd TEXT, git_repo_root TEXT)"
            )
            connection.execute(
                "INSERT INTO sessions VALUES (?, ?, ?)",
                ("safe-session", None, "/private/repos/FallbackProject"),
            )
        with mock.patch.dict(os.environ, {"HERMES_HOME": hermes_home}):
            self.assertEqual(hook.project_label("safe-session"), "FallbackProject")

    def test_same_kind_activity_advances_updated_at_without_resetting_started_at(self):
        with mock.patch.object(hook, "utc_timestamp", side_effect=[
                "2030-01-02T03:04:05Z", "2030-01-02T03:04:06Z"]):
            self.event("pre_llm_call")
            self.event("pre_llm_call")
        with open(self.path, encoding="utf-8") as source:
            activity = json.load(source)["activity"]
        self.assertEqual(activity["startedAt"], "2030-01-02T03:04:05Z")
        self.assertEqual(activity["updatedAt"], "2030-01-02T03:04:06Z")

    def test_activity_history_is_bounded_and_contains_only_safe_events(self):
        for index in range(9):
            self.event("pre_tool_call", tool_name="shell --secret-%d" % index,
                       tool_call_id=str(index), command="PRIVATE COMMAND")
            self.event("post_tool_call", tool_name="shell --secret-%d" % index,
                       tool_call_id=str(index), result={"error": "PRIVATE ERROR"})
        with open(self.path, encoding="utf-8") as source:
            activity = json.load(source)["activity"]
        self.assertLessEqual(len(activity["recentEvents"]), 6)
        serialized = json.dumps(activity)
        self.assertNotIn("PRIVATE", serialized)
        self.assertNotIn("--secret", serialized)
        self.assertEqual(set(activity), {
            "projectLabel", "activityKind", "activityLabel", "activeSubagents",
            "startedAt", "updatedAt", "recentEvents",
        })

    def test_edge_trigger_error_and_finish(self):
        self.event("pre_llm_call")
        self.event("pre_llm_call")
        self.assertEqual(len(self.pushes), 2)
        self.event("pre_tool_call", tool_name="shell", tool_call_id="a")
        self.event("post_tool_call", tool_name="shell", tool_call_id="a", result={"status": "error", "raw": "secret"})
        self.assertEqual(self.pushes[-1], ("error", hook.DETAILS["error"]))
        self.event("post_llm_call")
        self.assertEqual(self.pushes[-1][0], "idle")

    def test_wire_payload_extra_status_and_json_result(self):
        self.event("pre_tool_call", extra={"tool_name": "web_search", "tool_call_id": "wire-a"})
        self.assertEqual(self.pushes[-1][0], "researching")
        self.event("post_tool_call", extra={"tool_call_id": "wire-a", "status": "failed", "result": '{"raw":"DO_NOT_LEAK"}'})
        self.assertEqual(self.pushes[-1], ("error", hook.DETAILS["error"]))
        self.assertNotIn("DO_NOT_LEAK", self.pushes[-1][1])
        self.event("pre_tool_call", extra={"tool_name": "shell", "tool_call_id": "wire-b"})
        self.event("post_tool_call", extra={"tool_call_id": "wire-b", "result": '{"status":"error","secret":"NO"}'})
        self.assertEqual(self.pushes[-1][0], "error")

    def test_wire_payload_json_result_exit_code(self):
        cases = (
            (0, "idle"),
            (11, "error"),
            (-11, "error"),
            (True, "idle"),
            (False, "idle"),
            ("11", "idle"),
            ("malformed", "idle"),
        )
        for index, (exit_code, expected) in enumerate(cases):
            with self.subTest(exit_code=exit_code):
                call_id = "exit-code-%s" % index
                self.event("pre_tool_call", extra={"tool_name": "terminal", "tool_call_id": call_id})
                self.event("post_tool_call", extra={
                    "tool_call_id": call_id,
                    "result": json.dumps({"exit_code": exit_code, "error": None}),
                })
                self.assertEqual(self.pushes[-1][0], expected)
                self.event("on_session_reset")

    def test_documented_subagent_wire_payloads_track_three_children(self):
        goals = ["SECRET GOAL %s" % index for index in range(3)]
        for index in range(3):
            hook.run({"hook_event_name": "subagent_start", "extra": {
                "parent_session_id": "parent", "child_session_id": "child-session-%s" % index,
                "child_subagent_id": "child-%s" % index, "child_role": "worker", "child_goal": goals[index]}})
        with open(self.path, encoding="utf-8") as source:
            session = json.load(source)["sessions"]["parent"]
        self.assertEqual(session["subagents"], [
            "child-session-0", "child-session-1", "child-session-2"])
        self.assertFalse(any(goal in detail for _, detail in self.pushes for goal in goals))

        statuses = ("completed", "failed", "interrupted")
        summaries = ["SECRET SUMMARY %s" % index for index in range(3)]
        for status, summary in zip(statuses, summaries):
            hook.run({"hook_event_name": "subagent_stop", "extra": {
                "parent_session_id": "parent", "child_role": "worker", "child_summary": summary,
                "child_status": status, "duration_ms": 20}})
        with open(self.path, encoding="utf-8") as source:
            session = json.load(source)["sessions"]["parent"]
        self.assertEqual(session["subagents"], [])
        self.assertEqual(session["phase"], "error")
        self.assertFalse(any(summary in detail for _, detail in self.pushes for summary in summaries))

    def test_desktop_subagent_start_and_stop_pair_by_child_session_id(self):
        self.event("subagent_start", child_session_id="child-session", child_subagent_id="sa-child")
        self.event("subagent_stop", child_session_id="child-session", child_status="completed")

        with open(self.path, encoding="utf-8") as source:
            data = json.load(source)
        self.assertEqual(data["sessions"]["s1"]["subagents"], [])
        self.assertNotEqual(hook.display_state(data), "syncing")

    def test_parallel_calls_remove_only_match(self):
        self.event("pre_tool_call", tool_name="web_search", tool_call_id="a")
        self.event("pre_tool_call", tool_name="shell_exec", tool_call_id="b")
        self.event("post_tool_call", tool_name="web_search", tool_call_id="a", success=True)
        self.assertEqual(self.pushes[-1][0], "executing")
        with open(self.path, encoding="utf-8") as source:
            active = json.load(source)["sessions"]["s1"]["active"]
        self.assertEqual(active, {"b": "executing"})

    def test_idless_tool_pair_removes_its_fallback(self):
        self.event("pre_tool_call", tool_name="shell")
        self.event("post_tool_call", tool_name="shell")
        with open(self.path, encoding="utf-8") as source:
            session = json.load(source)["sessions"]["s1"]
        self.assertEqual(session["active"], {})
        self.assertEqual(session["fallback_tools"], {})

    def test_three_concurrent_idless_tools_finish_fifo(self):
        for _ in range(3):
            self.event("pre_tool_call", tool_name="SHELL")
        with open(self.path, encoding="utf-8") as source:
            session = json.load(source)["sessions"]["s1"]
        self.assertEqual(len(session["active"]), 3)
        self.assertEqual(session["fallback_tools"]["terminal"], ["fallback:1", "fallback:2", "fallback:3"])
        for remaining in (2, 1, 0):
            self.event("post_tool_call", tool_name="shell")
            with open(self.path, encoding="utf-8") as source:
                session = json.load(source)["sessions"]["s1"]
            self.assertEqual(len(session["active"]), remaining)

    def test_three_concurrent_idless_subagents_stop_fifo(self):
        for _ in range(3):
            self.event("subagent_start")
        with open(self.path, encoding="utf-8") as source:
            session = json.load(source)["sessions"]["s1"]
        self.assertEqual(session["subagents"], [
            "fallback-subagent:1", "fallback-subagent:2", "fallback-subagent:3"])

        for remaining in (2, 1, 0):
            self.event("subagent_stop")
            with open(self.path, encoding="utf-8") as source:
                data = json.load(source)
            self.assertEqual(len(data["sessions"]["s1"]["subagents"]), remaining)
            self.assertEqual(hook.display_state(data), "syncing" if remaining else "writing")

    def test_replayed_unknown_subagent_stop_preserves_other_child(self):
        self.event("subagent_start", child_subagent_id="first")
        self.event("subagent_start", child_subagent_id="second")
        self.event("subagent_stop", child_subagent_id="first")
        with mock.patch("sys.stderr", io.StringIO()) as stderr:
            self.event("subagent_stop", child_subagent_id="first", child_goal="SECRET")
        with open(self.path, encoding="utf-8") as source:
            subagents = json.load(source)["sessions"]["s1"]["subagents"]
        self.assertEqual(subagents, ["second"])
        self.assertNotIn("first", stderr.getvalue())
        self.assertNotIn("SECRET", stderr.getvalue())

    def test_unsupported_locking_platform_fails_open(self):
        with mock.patch.object(hook, "fcntl", None), mock.patch("sys.stdin", io.StringIO(json.dumps({"hook_event_name": "pre_llm_call"}))), mock.patch("sys.stderr", io.StringIO()) as stderr:
            self.assertEqual(hook.main(), 0)
        self.assertIn("unsupported", stderr.getvalue())

    def test_bearer_header(self):
        self.push_patch.stop()
        captured = {}
        def open_request(request, timeout):
            captured["authorization"] = request.get_header("Authorization")
            captured["timeout"] = timeout
            return Response()
        with mock.patch.dict(os.environ, {"STAR_OFFICE_API_TOKEN": "abc", "STAR_OFFICE_HOOK_TIMEOUT": "0.2"}), mock.patch.object(hook.urllib.request, "urlopen", side_effect=open_request):
            hook.push("idle", "generic")
        self.assertEqual(captured, {"authorization": "Bearer abc", "timeout": 0.2})
        self.push_patch.start()

    def test_malformed_and_unreachable_fail_open(self):
        self.push_patch.stop()
        with mock.patch("sys.stdin", io.StringIO("not-json")), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(hook.main(), 0)
        with mock.patch("sys.stdin", io.StringIO(json.dumps({"hook_event_name": "pre_llm_call"}))), mock.patch.object(hook, "push", side_effect=urllib.error.URLError("down")), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(hook.main(), 0)
        self.push_patch.start()

    def test_invalid_timeout_uses_bounded_default(self):
        self.push_patch.stop()
        captured = []
        with mock.patch.dict(os.environ, {"STAR_OFFICE_HOOK_TIMEOUT": "not-a-number"}), \
                mock.patch.object(hook.urllib.request, "urlopen", side_effect=lambda request, timeout: captured.append(timeout) or Response()):
            hook.push("idle", "generic")
        self.assertEqual(captured, [0.75])
        self.push_patch.start()

    def test_failed_delivery_is_retried_by_a_later_event(self):
        self.push_patch.stop()
        attempts = []

        def flaky_push(state, detail, activity=None):
            attempts.append(state)
            if len(attempts) == 1:
                raise urllib.error.URLError("down")

        with mock.patch.object(hook, "push", side_effect=flaky_push), mock.patch("sys.stderr", io.StringIO()):
            payload = {"hook_event_name": "pre_llm_call", "session_id": "retry"}
            hook.run(payload)
            hook.run(payload)
        self.assertEqual(attempts, ["writing", "writing"])
        with open(self.path, encoding="utf-8") as source:
            self.assertEqual(json.load(source)["last_push"]["state"], "writing")
        self.push_patch.start()

    def test_http_delivery_does_not_hold_file_lock(self):
        self.push_patch.stop()
        first_entered = threading.Event()
        second_entered = threading.Event()
        release = threading.Event()
        calls = []

        def delayed_push(state, detail, activity=None):
            calls.append(state)
            if len(calls) == 1:
                first_entered.set()
            else:
                second_entered.set()
            self.assertTrue(release.wait(2))

        with mock.patch.object(hook, "push", side_effect=delayed_push):
            first = threading.Thread(target=hook.run, args=({"hook_event_name": "pre_llm_call", "session_id": "a"},))
            first.start()
            self.assertTrue(first_entered.wait(1))
            second = threading.Thread(target=hook.run, args=({"hook_event_name": "pre_tool_call", "session_id": "b", "tool_name": "shell", "tool_call_id": "b"},))
            second.start()
            concurrent = second_entered.wait(1)
            release.set()
            first.join(2)
            second.join(2)
        self.assertTrue(concurrent, "second HTTP delivery waited on the first process's file lock")
        self.assertEqual(set(calls), {"writing", "executing"})
        self.push_patch.start()

    def test_reversed_http_completion_reconciles_latest_desired_state(self):
        self.push_patch.stop()
        writing_entered = threading.Event()
        allow_writing_to_finish = threading.Event()
        executing_finished = threading.Event()
        backend = {"state": None}
        calls = []

        def reversed_push(state, detail, activity=None):
            calls.append(state)
            if state == "writing":
                writing_entered.set()
                self.assertTrue(allow_writing_to_finish.wait(2))
            backend["state"] = state
            if state == "executing" and not executing_finished.is_set():
                executing_finished.set()

        with mock.patch.object(hook, "push", side_effect=reversed_push):
            older = threading.Thread(target=hook.run, args=({
                "hook_event_name": "pre_llm_call", "session_id": "race",
            },))
            older.start()
            self.assertTrue(writing_entered.wait(1))

            newer = threading.Thread(target=hook.run, args=({
                "hook_event_name": "pre_tool_call", "session_id": "race",
                "tool_name": "shell", "tool_call_id": "newer",
            },))
            newer.start()
            self.assertTrue(executing_finished.wait(1))
            newer.join(2)
            allow_writing_to_finish.set()
            older.join(2)

        self.assertFalse(older.is_alive())
        self.assertFalse(newer.is_alive())
        self.assertEqual(calls, ["writing", "executing", "executing"])
        self.assertEqual(backend["state"], "executing")
        with open(self.path, encoding="utf-8") as source:
            data = json.load(source)
        self.assertEqual(data["desired"]["update"]["state"], "executing")
        self.assertEqual(data["last_push"]["state"], "executing")
        self.push_patch.start()

    def test_concurrent_invocation_state_integrity(self):
        self.push_patch.stop()
        env = os.environ.copy()
        env.update({"STAR_OFFICE_HOOK_STATE_FILE": self.path, "STAR_OFFICE_URL": "http://127.0.0.1:1", "STAR_OFFICE_HOOK_TIMEOUT": "0.05"})
        processes = []
        for index in range(12):
            payload = json.dumps({"hook_event_name": "pre_tool_call", "session_id": "parallel", "tool_name": "shell", "tool_call_id": str(index)})
            process = subprocess.Popen([sys.executable, HOOK_PATH], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            processes.append((process, payload))
        for process, payload in processes:
            process.communicate(payload, timeout=5)
            self.assertEqual(process.returncode, 0)
        with open(self.path, encoding="utf-8") as source:
            data = json.load(source)
        self.assertEqual(len(data["sessions"]["parallel"]["active"]), 12)
        self.push_patch.start()


class HookConfigTests(unittest.TestCase):
    def test_example_has_hermes_list_and_scalar_command_shape(self):
        path = os.path.join(ROOT, "integrations", "hermes", "hooks.example.yaml")
        with open(path, encoding="utf-8") as source:
            content = source.read()
        events = ("pre_llm_call", "post_llm_call", "pre_tool_call", "post_tool_call",
                  "on_session_start", "on_session_end", "on_session_finalize", "on_session_reset",
                  "subagent_start", "subagent_stop")
        lines = content.splitlines()
        for event in events:
            index = lines.index("  %s:" % event)
            command_line = lines[index + 1]
            self.assertTrue(command_line.startswith("    - command: "))
            scalar = command_line.split(": ", 1)[1]
            self.assertFalse(scalar.startswith("["))
            command = scalar[1:-1]
            argv = shlex.split(command)
            self.assertEqual(len(argv), 2)
            self.assertTrue(argv[0].endswith("/.venv/bin/python"))
            self.assertTrue(argv[1].endswith("/integrations/hermes/star_office_hook.py"))


if __name__ == "__main__":
    unittest.main()
