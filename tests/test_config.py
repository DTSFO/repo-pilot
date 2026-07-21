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

    def test_fetch_hosts_are_normalized(self) -> None:
        settings = Settings(
            _env_file=None,
            allowed_fetch_hosts=" EXAMPLE.com,docs.example.com,example.com ",
        )

        self.assertEqual(
            settings.fetch_host_allowlist,
            frozenset({"example.com", "docs.example.com"}),
        )


if __name__ == "__main__":
    unittest.main()
