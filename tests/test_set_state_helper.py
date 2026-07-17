import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(__file__))
SPEC = importlib.util.spec_from_file_location("state_helper", os.path.join(ROOT, "set_state.py"))
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


class StateHelperTests(unittest.TestCase):
    def test_desktop_pet_compatible_states(self):
        self.assertEqual(set(helper.VALID_STATES), {"idle", "writing", "receiving", "replying", "researching", "executing", "syncing", "error"})

    def test_atomic_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            with mock.patch.object(helper, "STATE_FILE", path), mock.patch.object(helper.os, "replace", wraps=os.replace) as replace:
                helper.save_state({"state": "idle", "detail": "ok"})
                replace.assert_called_once()
            with open(path, encoding="utf-8") as source:
                self.assertEqual(json.load(source)["detail"], "ok")
            self.assertEqual(os.listdir(directory), ["state.json"])

    def test_cli_preserves_receiving_and_replying_animations(self):
        script = os.path.join(ROOT, "set_state.py")
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            environment = os.environ.copy()
            environment["STAR_OFFICE_STATE_FILE"] = path
            for state in ("receiving", "replying"):
                result = subprocess.run(
                    [sys.executable, script, state, "desktop pet"],
                    capture_output=True, text=True, env=environment, timeout=5,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                with open(path, encoding="utf-8") as source:
                    self.assertEqual(json.load(source)["state"], state)


if __name__ == "__main__":
    unittest.main()
