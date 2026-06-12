from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import ccusage

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


def _creds(expires_at: int) -> dict:
    return {
        "claudeAiOauth": {
            "accessToken": "old-token",
            "refreshToken": "old-refresh",
            "expiresAt": expires_at,
            "scopes": ["user:inference"],
            "subscriptionType": "max",
        },
        "otherTopLevel": "keep-me",
    }


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def _json_response(payload: dict) -> _FakeResponse:
    return _FakeResponse(json.dumps(payload).encode())


REFRESH_RESULT = {
    "access_token": "new-token",
    "refresh_token": "new-refresh",
    "expires_in": 28800,
}


class FetchUsageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.credfile = Path(self._tmp.name) / ".credentials.json"
        patcher = mock.patch.object(ccusage, "CREDENTIALS_FILE", self.credfile)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_creds(self, expires_at: int):
        self.credfile.write_text(json.dumps(_creds(expires_at)))

    def test_valid_token_skips_refresh(self):
        self._write_creds(int(time.time() * 1000) + 3_600_000)
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            self.assertEqual(req.headers["Authorization"], "Bearer old-token")
            return _json_response({"five_hour": {"utilization": 4.0}})

        with mock.patch.object(ccusage.urllib.request, "urlopen", fake_urlopen):
            data = ccusage.fetch_usage()

        self.assertEqual(data, {"five_hour": {"utilization": 4.0}})
        self.assertEqual(calls, [USAGE_URL])

    def test_expired_token_refreshes_and_persists_rotated_credentials(self):
        self._write_creds(0)
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            if req.full_url == ccusage.TOKEN_URL:
                self.assertEqual(
                    json.loads(req.data),
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": "old-refresh",
                        "client_id": ccusage.CLIENT_ID,
                    },
                )
                return _json_response(REFRESH_RESULT)
            self.assertEqual(req.headers["Authorization"], "Bearer new-token")
            return _json_response({"five_hour": {"utilization": 4.0}})

        with mock.patch.object(ccusage.urllib.request, "urlopen", fake_urlopen):
            ccusage.fetch_usage()

        self.assertEqual(calls, [ccusage.TOKEN_URL, USAGE_URL])

        on_disk = json.loads(self.credfile.read_text())
        oauth = on_disk["claudeAiOauth"]
        self.assertEqual(oauth["accessToken"], "new-token")
        self.assertEqual(oauth["refreshToken"], "new-refresh")
        self.assertGreater(oauth["expiresAt"], time.time() * 1000)
        # Fields not returned by the token endpoint must survive the rewrite
        self.assertEqual(oauth["scopes"], ["user:inference"])
        self.assertEqual(oauth["subscriptionType"], "max")
        self.assertEqual(on_disk["otherTopLevel"], "keep-me")
        self.assertEqual(self.credfile.stat().st_mode & 0o777, 0o600)

    def test_rejected_token_retries_once_after_refresh(self):
        self._write_creds(int(time.time() * 1000) + 3_600_000)
        state = {"rejected": False}

        def fake_urlopen(req, timeout=None):
            if req.full_url == ccusage.TOKEN_URL:
                return _json_response(REFRESH_RESULT)
            if not state["rejected"]:
                state["rejected"] = True
                raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))
            self.assertEqual(req.headers["Authorization"], "Bearer new-token")
            return _json_response({"ok": True})

        with mock.patch.object(ccusage.urllib.request, "urlopen", fake_urlopen):
            self.assertEqual(ccusage.fetch_usage(), {"ok": True})

        on_disk = json.loads(self.credfile.read_text())
        self.assertEqual(on_disk["claudeAiOauth"]["accessToken"], "new-token")

    def test_persistent_rejection_raises(self):
        self._write_creds(int(time.time() * 1000) + 3_600_000)

        def fake_urlopen(req, timeout=None):
            if req.full_url == ccusage.TOKEN_URL:
                return _json_response(REFRESH_RESULT)
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))

        with mock.patch.object(ccusage.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(urllib.error.HTTPError):
                ccusage.fetch_usage()

    def test_expired_token_without_refresh_token_raises(self):
        creds = _creds(0)
        del creds["claudeAiOauth"]["refreshToken"]
        self.credfile.write_text(json.dumps(creds))

        with self.assertRaisesRegex(RuntimeError, "no refresh token"):
            ccusage.fetch_usage()

    def test_refresh_endpoint_error_raises_runtime_error(self):
        self._write_creds(0)

        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, io.BytesIO(b""))

        with mock.patch.object(ccusage.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaisesRegex(RuntimeError, "Token refresh failed \\(429\\)"):
                ccusage.fetch_usage()


class BuildUsageJsonTests(unittest.TestCase):
    def test_maps_buckets_and_extra_usage(self):
        api_data = {
            "five_hour": {"utilization": 35.0, "resets_at": "2026-06-12T00:00:00+00:00"},
            "seven_day": {"utilization": 14.0, "resets_at": None},
            "seven_day_sonnet": None,
            "seven_day_opus": None,
            "extra_usage": {"is_enabled": True, "monthly_limit": 100000},
        }
        result = ccusage.build_usage_json(api_data, "max_20x")
        self.assertEqual(result["plan"], "max_20x")
        self.assertEqual(result["5h"], {"pct": 35.0, "resets_at": "2026-06-12T00:00:00+00:00"})
        self.assertEqual(result["7d"], {"pct": 14.0, "resets_at": None})
        self.assertNotIn("7d_sonnet", result)
        self.assertEqual(result["extra_usage"], {"is_enabled": True, "monthly_limit": 100000})


if __name__ == "__main__":
    unittest.main()
