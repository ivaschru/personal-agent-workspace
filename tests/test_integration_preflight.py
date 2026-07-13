"""Проверяет переносимый реестр сервисных интеграций."""

from __future__ import annotations

import importlib.util
import json
import tempfile
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
                "email-mailbox",
                "external-file-storage",
                "gas-pravosudie",
                "gosuslugi",
                "max-messenger",
                "ozon-buyer-search",
                "russian-post-registered-mail",
                "t-bank",
                "telegram-messenger",
                "trelio",
            },
        )

    def test_external_storage_setup_is_local_and_enables_integration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            selected = Path(temporary) / "synced-folder"
            root.mkdir()
            selected.mkdir()
            workspace = {
                "initialized": True,
                "features": {
                    "externalIntegrations": {
                        "setupMode": "on-demand",
                        "enabled": [],
                    }
                },
            }
            (root / "workspace.json").write_text(
                json.dumps(workspace), encoding="utf-8"
            )

            resolved = PREFLIGHT.setup_external_storage(root, workspace, selected)

            self.assertEqual(resolved, selected.resolve())
            local_config = root / ".local/external-file-storage.json"
            self.assertTrue(local_config.exists())
            self.assertEqual(
                json.loads(local_config.read_text(encoding="utf-8"))["root"],
                str(selected.resolve()),
            )
            saved_workspace = json.loads(
                (root / "workspace.json").read_text(encoding="utf-8")
            )
            self.assertIn(
                "external-file-storage",
                PREFLIGHT.enabled_integrations(saved_workspace),
            )
            self.assertEqual(list(selected.iterdir()), [])

    def test_external_storage_preflight_does_not_create_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            selected = Path(temporary)
            ready, message = PREFLIGHT.storage_status(selected)

            self.assertTrue(ready)
            self.assertIn("папка доступна", message)
            self.assertEqual(list(selected.iterdir()), [])

    def test_external_storage_must_not_overlap_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            selected = root / "external-files"
            selected.mkdir(parents=True)
            workspace = {"initialized": True}

            with self.assertRaisesRegex(ValueError, "не должна содержать Workspace"):
                PREFLIGHT.setup_external_storage(root, workspace, selected)

            self.assertFalse((root / ".local").exists())

    def test_missing_external_storage_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing"
            ready, message = PREFLIGHT.storage_status(missing)

            self.assertFalse(ready)
            self.assertIn("папка не найдена", message)

    def test_legacy_integration_list_is_migrated_during_setup(self) -> None:
        workspace = {
            "features": {"externalIntegrations": ["telegram-messenger"]}
        }

        PREFLIGHT.enable_integration(workspace, "external-file-storage")

        self.assertEqual(
            workspace["features"]["externalIntegrations"],
            {
                "setupMode": "on-demand",
                "enabled": ["external-file-storage", "telegram-messenger"],
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

    def test_trelio_policy_is_mcp_only(self) -> None:
        text = (SCRIPT.parents[1] / "skills/trelio/SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("https://trelio.ru/mcp", text)
        self.assertIn("Не использовать браузерный интерфейс", text)

    def test_telegram_does_not_bundle_application_credentials(self) -> None:
        root = SCRIPT.parents[1]
        for path in (root / "skills/telegram-messenger").rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"TELEGRAM_API_ID\s*=\s*\d+")
            self.assertNotRegex(text, r"TELEGRAM_API_HASH\s*=\s*[0-9a-fA-F]{20,}")

    def test_email_mailbox_has_no_personal_configuration(self) -> None:
        root = SCRIPT.parents[1]
        example = (
            root / "skills/email-mailbox/references/accounts.example.toml"
        ).read_text(encoding="utf-8")
        self.assertIn("owner@example.com", example)
        self.assertNotIn("refresh_token = \"ya29.", example)


if __name__ == "__main__":
    unittest.main()
