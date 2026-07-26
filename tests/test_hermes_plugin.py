import importlib.util
import io
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
PLUGIN_PATH = ROOT / "integrations" / "hermes" / "plugin" / "star-office-ui-status" / "__init__.py"
BRIDGE_PATH = ROOT / "integrations" / "hermes" / "star_office_hook.py"


def load_plugin(path=PLUGIN_PATH, name="star_office_ui_status_plugin"):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeContext:
    def __init__(self):
        self.hooks = {}

    def register_hook(self, name, callback):
        if name in self.hooks:
            raise AssertionError("duplicate hook: " + name)
        self.hooks[name] = callback


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.plugin = load_plugin(name="star_office_ui_status_plugin_" + self._testMethodName)
        self.context = FakeContext()
        with mock.patch.dict(os.environ, {"HERMES_DESKTOP": "1"}, clear=False):
            self.plugin.register(self.context)

    def test_desktop_registers_exactly_expected_keyword_hooks(self):
        # Hermes PluginManager.invoke_hook calls callbacks as cb(**kwargs).
        self.assertEqual(tuple(self.context.hooks), self.plugin.HOOK_EVENTS)
        self.assertFalse(hasattr(self.context, "tools"))

    def test_non_desktop_registers_no_hooks(self):
        context = FakeContext()
        with mock.patch.dict(os.environ, {}, clear=True):
            self.plugin.register(context)
        self.assertEqual(context.hooks, {})

    def test_tool_payload_is_allowlisted_and_redacted(self):
        bridge = mock.Mock()
        with mock.patch.object(self.plugin, "_load_bridge", return_value=bridge):
            result = self.context.hooks["pre_tool_call"](
                tool_name="terminal", session_id="session", task_id="task",
                tool_call_id="call", turn_id="turn",
                args={"command": "codex exec"}, command="SECRET",
                prompt="SECRET", child_goal="SECRET", child_summary="SECRET",
                conversation_history=["SECRET"], unrelated="SECRET")
        self.assertIsNone(result)
        payload = bridge.run.call_args.args[0]
        self.assertEqual(payload["hook_event_name"], "pre_tool_call")
        self.assertEqual(payload["tool_name"], "terminal")
        self.assertEqual(payload["session_id"], "session")
        self.assertEqual(payload["turn_id"], "turn")
        self.assertEqual(payload["tool_input"], {"command": "codex exec"})
        self.assertEqual(payload["extra"], {"task_id": "task", "tool_call_id": "call"})
        self.assertNotIn("SECRET", repr(payload))

    def test_pre_llm_payload_forwards_review_identity_fields(self):
        bridge = mock.Mock()
        with mock.patch.object(self.plugin, "_load_bridge", return_value=bridge):
            self.context.hooks["pre_llm_call"](
                session_id="session", turn_id="review-turn",
                user_message="review marker", conversation_history=["PRIVATE"])
        payload = bridge.run.call_args.args[0]
        self.assertEqual(payload["turn_id"], "review-turn")
        self.assertEqual(payload["user_message"], "review marker")
        self.assertNotIn("conversation_history", repr(payload))

    def test_post_tool_payload_keeps_ids_status_and_result_only(self):
        bridge = mock.Mock()
        raw_result = {"status": "error", "raw": "used only by bridge"}
        with mock.patch.object(self.plugin, "_load_bridge", return_value=bridge):
            self.context.hooks["post_tool_call"](
                tool_name="search_files", session_id="session", parent_session_id="parent",
                task_id="task", tool_call_id="tool-call", call_id="call",
                correlation_id="correlation", id="id",
                child_session_id="child-session", child_subagent_id="child-agent",
                child_status="failed", status="failed", success=False, is_error=True,
                error="generic error", result=raw_result, args={"query": "SECRET"})
        payload = bridge.run.call_args.args[0]
        self.assertEqual(payload["tool_name"], "search_files")
        self.assertEqual(payload["session_id"], "session")
        self.assertEqual(payload["extra"]["result"], raw_result)
        self.assertEqual(set(payload["extra"]), set(self.plugin._EXTRA_FIELDS))
        self.assertNotIn("args", payload["extra"])

    def test_callback_swallows_bridge_errors(self):
        bridge = mock.Mock()
        bridge.run.side_effect = RuntimeError("sensitive failure details")
        with mock.patch.object(self.plugin, "_load_bridge", return_value=bridge), \
                mock.patch("sys.stderr", io.StringIO()) as stderr:
            result = self.context.hooks["post_llm_call"](user_message="SECRET")
        self.assertIsNone(result)
        self.assertEqual(stderr.getvalue(), "star-office-plugin: status observer failed\n")

    def test_callback_propagates_keyboard_interrupt(self):
        bridge = mock.Mock()
        bridge.run.side_effect = KeyboardInterrupt()
        with mock.patch.object(self.plugin, "_load_bridge", return_value=bridge):
            with self.assertRaises(KeyboardInterrupt):
                self.context.hooks["post_llm_call"]()

    def test_bridge_load_retries_after_transient_failure(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
                self.plugin, "_bridge_path",
                side_effect=[Path(directory) / "missing.py", BRIDGE_PATH]):
            with self.assertRaises(FileNotFoundError):
                self.plugin._load_bridge()
            bridge = self.plugin._load_bridge()
        self.assertEqual(bridge.__file__, str(BRIDGE_PATH))
        self.assertIs(self.plugin._load_bridge(), bridge)

    def test_bridge_path_is_repository_relative_and_symlink_compatible(self):
        self.assertEqual(self.plugin._bridge_path(), BRIDGE_PATH)
        with tempfile.TemporaryDirectory() as directory:
            link = Path(directory) / "star-office-ui-status"
            os.symlink(PLUGIN_PATH.parent, link, target_is_directory=True)
            linked = load_plugin(link / "__init__.py", name="star_office_ui_status_linked")
            self.assertEqual(linked._bridge_path(), BRIDGE_PATH)
            self.assertEqual(linked._load_bridge().__file__, str(BRIDGE_PATH))

    def test_copy_install_layout_resolves_and_loads_bridge(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            plugin_dir = home / "plugins" / "star-office-ui-status"
            plugin_dir.mkdir(parents=True)
            shutil.copy2(PLUGIN_PATH, plugin_dir / "__init__.py")
            shutil.copy2(BRIDGE_PATH, home / "star_office_hook.py")
            copied = load_plugin(
                plugin_dir / "__init__.py", name="star_office_ui_status_copied")
            expected_bridge = (home / "star_office_hook.py").resolve()
            self.assertEqual(copied._bridge_path(), expected_bridge)
            self.assertEqual(copied._load_bridge().__file__, str(expected_bridge))


if __name__ == "__main__":
    unittest.main()
