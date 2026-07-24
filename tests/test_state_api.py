import importlib
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "backend"))
app_module = importlib.import_module("app")


class StateApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.temp.name, "state.json")
        self.original = {"state": "idle", "detail": "original", "progress": 0}
        with open(self.state_file, "w", encoding="utf-8") as output:
            json.dump(self.original, output)
        self.file_patch = mock.patch.object(app_module, "STATE_FILE", self.state_file)
        self.file_patch.start()
        self.env_patch = mock.patch.dict(os.environ, {}, clear=False)
        self.env_patch.start()
        os.environ.pop("STAR_OFFICE_API_TOKEN", None)
        self.client = app_module.app.test_client()

    def tearDown(self):
        self.env_patch.stop()
        self.file_patch.stop()
        self.temp.cleanup()

    def saved(self):
        with open(self.state_file, encoding="utf-8") as source:
            return json.load(source)

    def test_valid_and_max_length(self):
        response = self.client.post("/set_state", json={"state": "writing", "detail": "x" * 500})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.saved()["state"], "writing")
        self.assertEqual(len(self.saved()["detail"]), 500)

    def test_activity_is_allowlisted_validated_and_saved_with_state(self):
        activity = {
            "projectLabel": "Star-Office-UI-Hermes",
            "activityKind": "files",
            "activityLabel": "File activity",
            "activeSubagents": 2,
            "startedAt": "2030-01-02T03:04:05Z",
            "updatedAt": "2030-01-02T03:05:06+00:00",
            "recentEvents": [{
                "kind": "files", "label": "File activity",
                "at": "2030-01-02T03:05:06Z",
                "command": "never persist",
            }],
            "prompt": "never persist",
        }
        response = self.client.post("/set_state", json={
            "state": "executing", "detail": "generic", "activity": activity,
        })
        self.assertEqual(response.status_code, 200)
        saved = self.saved()["activity"]
        self.assertEqual(set(saved), {
            "projectLabel", "activityKind", "activityLabel", "activeSubagents",
            "startedAt", "updatedAt", "recentEvents",
        })
        self.assertEqual(set(saved["recentEvents"][0]), {"kind", "label", "at"})
        self.assertNotIn("never persist", json.dumps(saved))
        self.assertEqual(self.client.get("/status").get_json()["activity"], saved)

    def test_invalid_activity_rejects_without_mutating_existing_state(self):
        invalid = [
            {"projectLabel": "/Users/private/project", "activityKind": "files"},
            {"projectLabel": "safe", "activityKind": "raw_tool_name"},
            {"projectLabel": "safe", "activityKind": "files",
             "activityLabel": "File activity", "activeSubagents": 100,
             "startedAt": "not-a-date", "updatedAt": "2030-01-01T00:00:00Z",
             "recentEvents": []},
        ]
        for activity in invalid:
            with self.subTest(activity=activity):
                before = self.saved()
                response = self.client.post("/set_state", json={
                    "state": "writing", "activity": activity,
                })
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.saved(), before)

    def test_existing_state_detail_post_remains_compatible(self):
        response = self.client.post("/set_state", json={"state": "writing", "detail": "safe generic"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.saved()["detail"], "safe generic")

    def test_status_strips_malformed_activity_loaded_from_disk(self):
        malformed = dict(self.original, activity={
            "projectLabel": "/private/path",
            "activityKind": "files",
            "activityLabel": "File activity",
            "activeSubagents": 0,
            "startedAt": "not-a-timestamp",
            "updatedAt": "2030-01-02T03:05:06Z",
            "recentEvents": [],
            "prompt": "DO_NOT_RETURN",
        })
        with open(self.state_file, "w", encoding="utf-8") as output:
            json.dump(malformed, output)
        response = self.client.get("/status")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("activity", response.get_json())
        self.assertNotIn("DO_NOT_RETURN", response.get_data(as_text=True))

    def test_missing_invalid_state_and_no_mutation(self):
        for body in ({"detail": "leak"}, {"state": 1, "detail": "leak"}, {"state": "replying", "detail": "leak"}):
            with self.subTest(body=body):
                before = self.saved()
                self.assertEqual(self.client.post("/set_state", json=body).status_code, 400)
                self.assertEqual(self.saved(), before)

    def test_json_object_and_detail_validation(self):
        for body in ([], {"state": "idle", "detail": 3}, {"state": "idle", "detail": "x" * 501}):
            with self.subTest(body=body):
                self.assertEqual(self.client.post("/set_state", json=body).status_code, 400)
        self.assertEqual(self.saved(), self.original)

    def test_auth_absent_invalid_valid(self):
        os.environ["STAR_OFFICE_API_TOKEN"] = "test-token"
        body = {"state": "idle"}
        self.assertEqual(self.client.post("/set_state", json=body).status_code, 401)
        self.assertEqual(self.client.post("/set_state", json=body, headers={"Authorization": "Bearer wrong"}).status_code, 401)
        self.assertEqual(self.client.post("/set_state", json=body, headers={"Authorization": "Bearer test-token"}).status_code, 200)

    def test_host_parser(self):
        self.assertEqual(app_module.parse_backend_host(None), "0.0.0.0")
        self.assertEqual(app_module.parse_backend_host(""), "127.0.0.1")
        self.assertEqual(app_module.parse_backend_host("127.0.0.1"), "127.0.0.1")
        self.assertEqual(app_module.parse_backend_host("2001:db8::1"), "2001:db8::1")
        self.assertEqual(app_module.parse_backend_host("office.example.com"), "office.example.com")
        self.assertEqual(app_module.parse_backend_host("999.999.999.999"), "127.0.0.1")
        self.assertEqual(app_module.parse_backend_host("localhost"), "localhost")
        self.assertEqual(app_module.parse_backend_host("http://bad"), "127.0.0.1")

    def test_backend_save_uses_atomic_replace(self):
        with mock.patch.object(app_module.os, "replace", wraps=os.replace) as replace:
            app_module.save_state({"state": "idle", "detail": "atomic"})
            replace.assert_called_once()
        self.assertEqual(self.saved()["detail"], "atomic")
        self.assertEqual(os.listdir(self.temp.name), ["state.json"])

    def test_concurrent_valid_writes_keep_json_complete_with_and_without_auth(self):
        for token in (None, "test-token"):
            with self.subTest(authenticated=bool(token)):
                if token:
                    os.environ["STAR_OFFICE_API_TOKEN"] = token
                else:
                    os.environ.pop("STAR_OFFICE_API_TOKEN", None)
                failures = []

                def write(index):
                    headers = {"Authorization": "Bearer " + token} if token else {}
                    with app_module.app.test_client() as client:
                        response = client.post("/set_state", json={"state": "writing", "detail": "write-%d" % index}, headers=headers)
                    if response.status_code != 200:
                        failures.append(response.status_code)
                    try:
                        self.saved()
                    except (OSError, ValueError, json.JSONDecodeError) as exc:
                        failures.append(str(exc))

                threads = [threading.Thread(target=write, args=(index,)) for index in range(24)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(5)
                self.assertFalse(failures)
                self.assertTrue(all(not thread.is_alive() for thread in threads))
                self.assertTrue(self.saved()["detail"].startswith("write-"))


if __name__ == "__main__":
    unittest.main()
