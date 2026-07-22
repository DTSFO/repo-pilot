from __future__ import annotations

import unittest
from pathlib import Path

from pydantic import SecretStr, ValidationError

from repopilot.config import Settings


class SettingsTest(unittest.TestCase):
    def test_default_mode_is_offline_and_secret_repr_is_masked(self) -> None:
        settings = Settings(
            _env_file=None,
            workspace_root=Path("."),
            llm_api_key=SecretStr("sk-test-secret-value"),
        )

        self.assertEqual(settings.provider, "deterministic")
        self.assertNotIn("sk-test-secret-value", repr(settings))
        self.assertTrue(settings.resolved_workspace_root.is_absolute())

    def test_openai_compatible_requires_complete_configuration(self) -> None:
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, provider="openai_compatible")

    def test_blank_optional_secrets_are_treated_as_unset(self) -> None:
        settings = Settings(_env_file=None, api_token="", llm_api_key="   ")

        self.assertIsNone(settings.api_token)
        self.assertIsNone(settings.llm_api_key)

    def test_fetch_hosts_are_normalized(self) -> None:
        settings = Settings(
            _env_file=None,
            allowed_fetch_hosts=" EXAMPLE.com,docs.example.com,example.com ",
        )

        self.assertEqual(
            settings.fetch_host_allowlist,
            frozenset({"example.com", "docs.example.com"}),
        )

    def test_streaming_and_granular_llm_timeout_defaults(self) -> None:
        settings = Settings(_env_file=None)

        self.assertTrue(settings.llm_streaming_enabled)
        self.assertTrue(settings.llm_stream_include_usage)
        self.assertEqual(settings.resolved_llm_connect_timeout_seconds, 10.0)
        self.assertEqual(settings.resolved_llm_read_timeout_seconds, 120.0)
        self.assertEqual(settings.resolved_llm_write_timeout_seconds, 30.0)
        self.assertEqual(settings.resolved_llm_pool_timeout_seconds, 10.0)
        self.assertEqual(settings.sse_poll_seconds, 0.2)
        self.assertEqual(settings.sse_heartbeat_seconds, 15.0)
        self.assertEqual(settings.daily_task_limit, 0)
        self.assertEqual(settings.daily_quota_timezone, "UTC")

    def test_daily_quota_timezone_must_be_valid(self) -> None:
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, daily_quota_timezone="Not/A_Timezone")

    def test_legacy_aggregate_timeout_and_granular_override_are_supported(self) -> None:
        settings = Settings(
            _env_file=None,
            llm_timeout_seconds=15,
            llm_read_timeout_seconds=90,
            llm_streaming_enabled=False,
        )

        self.assertEqual(settings.resolved_llm_connect_timeout_seconds, 15)
        self.assertEqual(settings.resolved_llm_read_timeout_seconds, 90)
        self.assertEqual(settings.resolved_llm_write_timeout_seconds, 15)
        self.assertEqual(settings.resolved_llm_pool_timeout_seconds, 15)
        self.assertFalse(settings.llm_streaming_enabled)


if __name__ == "__main__":
    unittest.main()
