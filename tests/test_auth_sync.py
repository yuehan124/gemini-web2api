import http.client
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

from gemini_web2api import authsync
from gemini_web2api.config import CONFIG
from gemini_web2api.server import GeminiHandler, ThreadedServer


SESSION_COOKIE = "SAPISID=abcd; __Secure-1PSID=xyz; __Secure-3PSID=zzz"
XSRF = "AOOhTtoken123"
BL = "boq_assistant-bard-web-server_20260716.08_p0"

VALID_PAYLOAD = {
    "cookie": SESSION_COOKIE,
    "sapisid": "abcd",
    "auth_user": "0",
    "xsrf_token": XSRF,
    "gemini_bl": BL,
}


class NormalizePayloadTests(unittest.TestCase):
    def test_accepts_valid_payload(self):
        payload, err = authsync.normalize_payload(dict(VALID_PAYLOAD))
        self.assertIsNone(err)
        self.assertEqual(payload["cookie"], SESSION_COOKIE)
        self.assertEqual(payload["sapisid"], "abcd")
        self.assertEqual(payload["auth_user"], "0")
        self.assertEqual(payload["xsrf_token"], XSRF)
        self.assertEqual(payload["gemini_bl"], BL)

    def test_rejects_non_dict(self):
        payload, err = authsync.normalize_payload(["nope"])
        self.assertIsNone(payload)
        self.assertIn("invalid JSON", err)

    def test_rejects_missing_cookie(self):
        raw = dict(VALID_PAYLOAD)
        raw.pop("cookie")
        payload, err = authsync.normalize_payload(raw)
        self.assertIsNone(payload)
        self.assertIn("cookie", err)

    def test_rejects_cookie_without_session_cookie(self):
        raw = dict(VALID_PAYLOAD)
        raw["cookie"] = "SAPISID=abcd; NID=1"
        payload, err = authsync.normalize_payload(raw)
        self.assertIsNone(payload)
        self.assertIn("session cookie", err)

    def test_rejects_cookie_without_sapisid(self):
        raw = dict(VALID_PAYLOAD)
        raw["cookie"] = "__Secure-1PSID=xyz"
        raw.pop("sapisid")
        payload, err = authsync.normalize_payload(raw)
        self.assertIsNone(payload)
        self.assertIn("SAPISID", err)

    def test_derives_sapisid_from_cookie(self):
        raw = dict(VALID_PAYLOAD)
        raw.pop("sapisid")
        payload, err = authsync.normalize_payload(raw)
        self.assertIsNone(err)
        self.assertEqual(payload["sapisid"], "abcd")

    def test_requires_xsrf_token(self):
        raw = dict(VALID_PAYLOAD)
        raw["xsrf_token"] = "   "
        payload, err = authsync.normalize_payload(raw)
        self.assertIsNone(payload)
        self.assertIn("xsrf_token", err)

    def test_integer_auth_user_is_stringified(self):
        raw = dict(VALID_PAYLOAD)
        raw["auth_user"] = 1
        payload, err = authsync.normalize_payload(raw)
        self.assertIsNone(err)
        self.assertEqual(payload["auth_user"], "1")

    def test_blank_auth_user_becomes_none(self):
        raw = dict(VALID_PAYLOAD)
        raw["auth_user"] = ""
        payload, err = authsync.normalize_payload(raw)
        self.assertIsNone(err)
        self.assertIsNone(payload["auth_user"])

    def test_missing_gemini_bl_stays_none(self):
        raw = dict(VALID_PAYLOAD)
        raw.pop("gemini_bl")
        payload, err = authsync.normalize_payload(raw)
        self.assertIsNone(err)
        self.assertIsNone(payload["gemini_bl"])


class HandleSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.auth_path = os.path.join(self.tmp.name, "gemini-auth.json")
        self.config_path = os.path.join(self.tmp.name, "config.json")
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump({"port": 8081, "cookie_file": None, "default_model": "gemini-3.6-flash"}, handle)

    def _payload(self):
        payload, err = authsync.normalize_payload(dict(VALID_PAYLOAD))
        self.assertIsNone(err)
        return payload

    def test_writes_auth_file_and_patches_config(self):
        result, err = authsync.handle_sync(self._payload(), self.auth_path, self.config_path)
        self.assertIsNone(err)

        self.assertTrue(os.path.exists(self.auth_path))
        with open(self.auth_path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["cookie"], SESSION_COOKIE)
        self.assertEqual(saved["sapisid"], "abcd")
        self.assertEqual(saved["xsrf_token"], XSRF)
        self.assertEqual(saved["gemini_bl"], BL)
        self.assertEqual(saved["auth_user"], "0")

        with open(self.config_path, encoding="utf-8") as handle:
            config = json.load(handle)
        self.assertEqual(config["xsrf_token"], XSRF)
        self.assertEqual(config["auth_user"], "0")
        self.assertEqual(config["gemini_bl"], BL)
        self.assertEqual(config["cookie_file"], self.auth_path)
        # Unrelated keys are preserved.
        self.assertEqual(config["port"], 8081)
        self.assertEqual(config["default_model"], "gemini-3.6-flash")

        self.assertEqual(result["config_file"], self.config_path)
        self.assertEqual(result["auth_file"], self.auth_path)
        self.assertIn("xsrf_token", result["updates"])

    def test_omits_gemini_bl_when_not_supplied(self):
        payload = self._payload()
        payload["gemini_bl"] = None
        result, err = authsync.handle_sync(payload, self.auth_path, self.config_path)
        self.assertIsNone(err)
        self.assertNotIn("gemini_bl", result["updates"])
        with open(self.config_path, encoding="utf-8") as handle:
            self.assertNotIn("gemini_bl", json.load(handle))

    def test_without_config_path_only_writes_auth_file(self):
        result, err = authsync.handle_sync(self._payload(), self.auth_path, None)
        self.assertIsNone(err)
        self.assertTrue(os.path.exists(self.auth_path))
        self.assertIsNone(result["config_file"])

    def test_unchanged_session_leaves_files_untouched(self):
        payload = self._payload()
        result, err = authsync.handle_sync(payload, self.auth_path, self.config_path)
        self.assertIsNone(err)
        self.assertTrue(result["changed"])

        auth_mtime = os.stat(self.auth_path).st_mtime_ns
        config_mtime = os.stat(self.config_path).st_mtime_ns

        result, err = authsync.handle_sync(payload, self.auth_path, self.config_path)
        self.assertIsNone(err)
        self.assertFalse(result["changed"])
        self.assertEqual(os.stat(self.auth_path).st_mtime_ns, auth_mtime)
        self.assertEqual(os.stat(self.config_path).st_mtime_ns, config_mtime)

    def test_volatile_cookies_are_not_persisted(self):
        payload = self._payload()
        payload["cookie"] = (
            "SAPISID=abcd; __Secure-1PSID=xyz; __Secure-1PSIDTS=rot1; AEC=rot2; NID=rot3"
        )
        result, err = authsync.handle_sync(payload, self.auth_path, self.config_path)
        self.assertIsNone(err)
        with open(self.auth_path, encoding="utf-8") as handle:
            saved = json.load(handle)["cookie"]
        self.assertNotIn("__Secure-1PSIDTS", saved)
        self.assertNotIn("AEC", saved)
        self.assertNotIn("NID", saved)
        self.assertIn("SAPISID=abcd", saved)
        self.assertIn("__Secure-1PSID=xyz", saved)

    def test_rotating_cookie_changes_do_not_rewrite_auth_file(self):
        payload = self._payload()
        payload["cookie"] = "SAPISID=abcd; __Secure-1PSID=xyz; __Secure-1PSIDTS=v1"
        authsync.handle_sync(payload, self.auth_path, self.config_path)
        auth_mtime = os.stat(self.auth_path).st_mtime_ns

        # Same session, only a volatile rotating cookie changed.
        payload["cookie"] = "SAPISID=abcd; __Secure-1PSID=xyz; __Secure-1PSIDTS=v2"
        result, err = authsync.handle_sync(payload, self.auth_path, self.config_path)
        self.assertIsNone(err)
        self.assertFalse(result["changed"])
        self.assertEqual(os.stat(self.auth_path).st_mtime_ns, auth_mtime)

    def test_changed_cookie_rewrites_auth_file(self):
        authsync.handle_sync(self._payload(), self.auth_path, self.config_path)

        payload = self._payload()
        payload["cookie"] = "SAPISID=new; __Secure-1PSID=xyz"
        result, err = authsync.handle_sync(payload, self.auth_path, self.config_path)
        self.assertIsNone(err)
        self.assertTrue(result["changed"])
        with open(self.auth_path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["cookie"], payload["cookie"])


class AuthSyncEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadedServer(("127.0.0.1", 0), GeminiHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        self.original_config = dict(CONFIG)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        self.auth_path = os.path.join(self.tmp.name, "gemini-auth.json")
        self.config_path = os.path.join(self.tmp.name, "config.json")
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump({"port": 8081, "cookie_file": None}, handle)

        CONFIG["api_keys"] = []
        CONFIG["log_requests"] = False
        CONFIG["sync_token"] = None
        CONFIG["cookie_file"] = self.auth_path

        self.config_patch = mock.patch("gemini_web2api.config._CONFIG_PATH", self.config_path)
        self.config_patch.start()

    def tearDown(self):
        self.config_patch.stop()
        CONFIG.clear()
        CONFIG.update(self.original_config)

    def post(self, payload, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        connection.request("POST", "/v1/auth/sync", body=json.dumps(payload), headers=request_headers)
        response = connection.getresponse()
        body = response.read().decode()
        connection.close()
        return response.status, body

    def test_sync_persists_session_and_updates_config(self):
        status, body = self.post(VALID_PAYLOAD)
        self.assertEqual(status, 200, body)

        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertEqual(data["auth_file"], self.auth_path)
        self.assertEqual(data["config_file"], self.config_path)

        with open(self.auth_path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["xsrf_token"], XSRF)

        with open(self.config_path, encoding="utf-8") as handle:
            config = json.load(handle)
        self.assertEqual(config["xsrf_token"], XSRF)
        self.assertEqual(config["cookie_file"], self.auth_path)

        # The running process picked the values up without a restart.
        self.assertEqual(CONFIG["xsrf_token"], XSRF)
        self.assertEqual(CONFIG["cookie_file"], self.auth_path)

    def test_rejects_invalid_payload(self):
        status, body = self.post({"cookie": "SAPISID=abc; __Secure-1PSID=1"})
        self.assertEqual(status, 400, body)
        self.assertIn("xsrf_token", body)
        self.assertFalse(os.path.exists(self.auth_path))

    def test_rejects_web_page_origin(self):
        status, body = self.post(VALID_PAYLOAD, {"Origin": "https://evil.example"})
        self.assertEqual(status, 403, body)
        self.assertFalse(os.path.exists(self.auth_path))

    def test_allows_extension_origin(self):
        status, body = self.post(VALID_PAYLOAD, {"Origin": "chrome-extension://abcdefghijklmnop"})
        self.assertEqual(status, 200, body)

    def test_requires_sync_token_when_configured(self):
        CONFIG["sync_token"] = "s3cret"

        status, body = self.post(VALID_PAYLOAD)
        self.assertEqual(status, 401, body)
        self.assertFalse(os.path.exists(self.auth_path))

        status, body = self.post(VALID_PAYLOAD, {"X-Sync-Token": "wrong"})
        self.assertEqual(status, 401, body)

        status, body = self.post(VALID_PAYLOAD, {"X-Sync-Token": "s3cret"})
        self.assertEqual(status, 200, body)

    def test_sync_ignores_api_keys(self):
        CONFIG["api_keys"] = ["sk-gemini"]
        status, body = self.post(VALID_PAYLOAD)
        self.assertEqual(status, 200, body)
        self.assertTrue(os.path.exists(self.auth_path))

    def test_invalid_json_body(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("POST", "/v1/auth/sync", body="{not json",
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        body = response.read().decode()
        connection.close()
        self.assertEqual(response.status, 400, body)


if __name__ == "__main__":
    unittest.main()
