"""Проверяет переносимый реестр сервисных интеграций."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/integration_preflight.py"
SPEC = importlib.util.spec_from_file_location("integration_preflight", SCRIPT)
assert SPEC and SPEC.loader
PREFLIGHT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREFLIGHT)


class IntegrationPreflightTests(unittest.TestCase):
    def test_expected_integrations_are_available(self) -> None:
        self.assertEqual(
            set(PREFLIGHT.INTEGRATIONS),
            {
                "gas-pravosudie",
                "gosuslugi",
                "max-messenger",
                "ozon-buyer-search",
                "russian-post-registered-mail",
                "t-bank",
            },
        )

    def test_current_object_schema_returns_enabled_integrations(self) -> None:
        workspace = {
            "features": {
                "externalIntegrations": {
                    "setupMode": "on-demand",
                    "enabled": ["gosuslugi", "ozon-buyer-search"],
                }
            }
        }
        self.assertEqual(
            PREFLIGHT.enabled_integrations(workspace),
            {"gosuslugi", "ozon-buyer-search"},
        )

    def test_legacy_list_schema_remains_readable(self) -> None:
        workspace = {"features": {"externalIntegrations": ["max-messenger"]}}
        self.assertEqual(
            PREFLIGHT.enabled_integrations(workspace), {"max-messenger"}
        )


if __name__ == "__main__":
    unittest.main()
