from __future__ import annotations

import io
import json
import os
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


class ClaudePathTests(unittest.TestCase):
    def test_config_dir_override_matches_claude_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "claude-config"
            with mock.patch.dict(
                os.environ, {"CLAUDE_CONFIG_DIR": str(config_dir)}, clear=False
            ):
                path = ccusage._resolve_claude_path(".credentials.json")

        self.assertEqual(path, config_dir / ".credentials.json")

    def test_config_dir_override_does_not_fall_back_to_wsl(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "missing-config"
            with (
                mock.patch.dict(
                    os.environ, {"CLAUDE_CONFIG_DIR": str(config_dir)}, clear=False
                ),
                mock.patch.object(ccusage.sys, "platform", "win32"),
                mock.patch.object(ccusage.subprocess, "run") as run_mock,
            ):
                path = ccusage._resolve_claude_path(".credentials.json")

        self.assertEqual(path, config_dir / ".credentials.json")
        run_mock.assert_not_called()


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
        self.assertEqual(list(self.credfile.parent.glob("..credentials.json.*")), [])

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

    def test_organization_oauth_denial_is_not_treated_as_token_rejection(self):
        self._write_creds(int(time.time() * 1000) + 3_600_000)
        calls = []
        denial = {
            "type": "error",
            "error": {
                "type": "permission_error",
                "message": "OAuth authentication is currently not allowed for this organization.",
                "details": {
                    "error_code": "oauth_not_allowed_for_organization",
                },
            },
        }

        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            raise urllib.error.HTTPError(
                req.full_url, 403, "Forbidden", {},
                io.BytesIO(json.dumps(denial).encode()),
            )

        with mock.patch.object(ccusage.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(ccusage.UsageUnavailableError) as raised:
                ccusage.fetch_usage()

        self.assertEqual(
            raised.exception.code, "oauth_not_allowed_for_organization"
        )
        self.assertEqual(calls, [USAGE_URL])

    def test_rejected_token_reloads_concurrently_updated_credentials(self):
        self._write_creds(int(time.time() * 1000) + 3_600_000)
        replacement = _creds(int(time.time() * 1000) + 3_600_000)
        replacement["claudeAiOauth"]["accessToken"] = "replacement-token"
        replacement["claudeAiOauth"]["refreshToken"] = "replacement-refresh"
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            if len(calls) == 1:
                self.credfile.write_text(json.dumps(replacement))
                raise urllib.error.HTTPError(
                    req.full_url, 401, "Unauthorized", {}, io.BytesIO(b"")
                )
            self.assertEqual(
                req.headers["Authorization"], "Bearer replacement-token"
            )
            return _json_response({"ok": True})

        with mock.patch.object(ccusage.urllib.request, "urlopen", fake_urlopen):
            self.assertEqual(ccusage.fetch_usage(), {"ok": True})

        self.assertEqual(calls, [USAGE_URL, USAGE_URL])

    def test_refresh_does_not_overwrite_concurrently_rotated_credentials(self):
        self._write_creds(0)
        original = json.loads(self.credfile.read_text())
        replacement = _creds(int(time.time() * 1000) + 3_600_000)
        replacement["claudeAiOauth"]["accessToken"] = "replacement-token"
        replacement["claudeAiOauth"]["refreshToken"] = "replacement-refresh"

        class RotatingResponse(_FakeResponse):
            def read(inner_self, *args):
                self.credfile.write_text(json.dumps(replacement))
                return super().read(*args)

        with mock.patch.object(
            ccusage.urllib.request,
            "urlopen",
            return_value=RotatingResponse(json.dumps(REFRESH_RESULT).encode()),
        ):
            result = ccusage.refresh_credentials(original)

        self.assertEqual(result, replacement)
        self.assertEqual(json.loads(self.credfile.read_text()), replacement)

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
    def test_reads_structured_limits_array(self):
        # Current API shape: session/weekly totals and the model-scoped Fable
        # quota all live in `limits`; the flat seven_day_* keys are ignored.
        api_data = {
            "seven_day_opus": None,
            "seven_day_sonnet": None,
            "iguana_necktie": None,
            "limits": [
                {"kind": "session", "percent": 3, "resets_at": "2026-07-04T05:10:00+00:00"},
                {"kind": "weekly_all", "percent": 33, "resets_at": "2026-07-07T13:00:00+00:00"},
                {"kind": "weekly_scoped", "percent": 58,
                 "resets_at": "2026-07-07T13:00:00+00:00",
                 "scope": {"model": {"id": None, "display_name": "Fable"}}},
            ],
            "extra_usage": {"is_enabled": True, "monthly_limit": 100000},
        }
        result = ccusage.build_usage_json(api_data, "max_20x")
        self.assertEqual(result["plan"], "max_20x")
        self.assertEqual(result["session"], {"pct": 3, "resets_at": "2026-07-04T05:10:00+00:00"})
        self.assertNotIn("5h", result)
        self.assertEqual(result["7d"], {"pct": 33, "resets_at": "2026-07-07T13:00:00+00:00"})
        self.assertEqual(result["7d_fable"], {"pct": 58, "resets_at": "2026-07-07T13:00:00+00:00"})
        self.assertEqual(result["extra_usage"], {"is_enabled": True, "monthly_limit": 100000})
        self.assertEqual(ccusage._bucket_display("7d_fable"), ("Week (Fable)", "fab"))
        # Buckets appear in a stable, sensible order.
        keys = [k for k, _ in ccusage._quota_buckets(result)]
        self.assertEqual(keys, ["session", "7d", "7d_fable"])

    def test_statusline_uses_semantic_session_bucket_for_reset(self):
        usage = {
            "plan": "max_20x",
            "session": {
                "pct": 3,
                "resets_at": "2099-01-01T00:00:00+00:00",
            },
            "7d": {"pct": 13, "resets_at": None},
        }
        status_input = {
            "model": {"display_name": "Test"},
            "workspace": {"current_dir": "/code/test"},
        }
        with (
            mock.patch.object(ccusage, "_get_cached_usage", return_value=usage),
            mock.patch.object(ccusage, "_TTY", False),
            mock.patch("sys.stdin", io.StringIO(json.dumps(status_input))),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            ccusage.cmd_statusline()

        output = stdout.getvalue()
        self.assertIn("sess:3%", output)
        self.assertIn("reset:", output)

    def test_scoped_limit_without_percent_is_skipped(self):
        api_data = {"limits": [
            {"kind": "weekly_scoped", "percent": None,
             "scope": {"model": {"display_name": "Opus"}}},
        ]}
        result = ccusage.build_usage_json(api_data, "max_20x")
        self.assertEqual([k for k, _ in ccusage._quota_buckets(result)], [])

    def test_rejects_missing_or_ill_typed_limits(self):
        for api_data in ({}, {"limits": None}, {"limits": {}}, {"limits": "bad"}):
            with self.subTest(api_data=api_data):
                with self.assertRaisesRegex(ValueError, "limits must be an array"):
                    ccusage.build_usage_json(api_data, "max_20x")

    def test_rejects_malformed_limit_entries(self):
        malformed = [
            "bad",
            {"kind": None, "percent": 10},
            {"kind": "weekly_scoped", "percent": 10, "scope": []},
            {"kind": "weekly_scoped", "percent": 10, "scope": {"model": {}}},
        ]
        for entry in malformed:
            with self.subTest(entry=entry):
                with self.assertRaisesRegex(ValueError, "Invalid usage response"):
                    ccusage.build_usage_json({"limits": [entry]}, "max_20x")

    def test_rejects_non_finite_or_out_of_range_percentages(self):
        for pct in (float("nan"), float("inf"), float("-inf"), -0.1, 100.1, True, "10"):
            with self.subTest(pct=pct):
                with self.assertRaisesRegex(ValueError, "percent must be between 0 and 100"):
                    ccusage.build_usage_json(
                        {"limits": [{"kind": "session", "percent": pct}]},
                        "max_20x",
                    )

    def test_rejects_invalid_reset_times(self):
        for resets_at in (42, "not-a-time", "2026-07-04T05:10:00"):
            with self.subTest(resets_at=resets_at):
                with self.assertRaisesRegex(ValueError, "reset time"):
                    ccusage.build_usage_json(
                        {
                            "limits": [
                                {
                                    "kind": "session",
                                    "percent": 10,
                                    "resets_at": resets_at,
                                }
                            ]
                        },
                        "max_20x",
                    )

    def test_unavailable_tombstone_preserves_success_time_not_quota_values(self):
        previous = {
            "plan": "max_20x",
            "updated_at": "2026-07-27T12:00:00+00:00",
            "session": {"pct": 3, "resets_at": None},
            "7d": {"pct": 13, "resets_at": None},
        }
        error = ccusage.UsageUnavailableError(
            "oauth_not_allowed_for_organization",
            "OAuth authentication is currently not allowed for this organization.",
        )

        result = ccusage.build_unavailable_usage(error, "max_20x", previous)

        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["last_success_at"], previous["updated_at"])
        self.assertEqual(
            result["unavailable"]["code"],
            "oauth_not_allowed_for_organization",
        )
        self.assertNotIn("session", result)
        self.assertNotIn("7d", result)
        self.assertEqual(list(ccusage._quota_buckets(result)), [])


class UnavailableCacheTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.usage_file = Path(self._tmp.name) / "usage-limits.json"
        patcher = mock.patch.object(ccusage, "USAGE_FILE", self.usage_file)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.error = ccusage.UsageUnavailableError(
            "oauth_not_allowed_for_organization",
            "OAuth authentication is currently not allowed for this organization.",
        )

    def test_stale_cache_is_replaced_when_refresh_is_unavailable(self):
        self.usage_file.write_text(json.dumps({
            "plan": "max_20x",
            "updated_at": "2026-07-27T12:00:00+00:00",
            "session": {"pct": 3, "resets_at": None},
        }))

        with (
            mock.patch.object(ccusage, "fetch_usage", side_effect=self.error),
            mock.patch.object(ccusage, "get_plan", return_value="max_20x"),
        ):
            result = ccusage._get_cached_usage(max_age=0)

        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("session", result)
        self.assertEqual(json.loads(self.usage_file.read_text()), result)

    def test_daemon_uses_hourly_retry_for_unavailable_usage(self):
        sleeps = []

        def stop_after_sleep(seconds):
            sleeps.append(seconds)
            raise KeyboardInterrupt

        with (
            mock.patch.object(ccusage, "fetch_usage", side_effect=self.error),
            mock.patch.object(ccusage, "get_plan", return_value="max_20x"),
            mock.patch.object(ccusage.signal, "signal"),
            mock.patch.object(ccusage.time, "sleep", side_effect=stop_after_sleep),
            mock.patch("sys.stdout", new_callable=io.StringIO),
            mock.patch("sys.stderr", new_callable=io.StringIO),
            self.assertRaises(KeyboardInterrupt),
        ):
            ccusage.cmd_daemon(interval=300)

        self.assertEqual(sleeps, [ccusage.UNAVAILABLE_RETRY_INTERVAL])
        cached = json.loads(self.usage_file.read_text())
        self.assertEqual(cached["status"], "unavailable")

    def test_statusline_reports_unavailable_without_quota_values(self):
        usage = ccusage.build_unavailable_usage(self.error, "max_20x")
        status_input = {
            "model": {"display_name": "Test"},
            "workspace": {"current_dir": "/code/test"},
        }
        with (
            mock.patch.object(ccusage, "_get_cached_usage", return_value=usage),
            mock.patch("sys.stdin", io.StringIO(json.dumps(status_input))),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            ccusage.cmd_statusline()

        output = stdout.getvalue()
        self.assertIn("usage:unavailable", output)
        self.assertNotIn("sess:", output)


if __name__ == "__main__":
    unittest.main()
