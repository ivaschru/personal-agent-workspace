"""Регрессии release manifest и файловых решений updater."""

from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


UPDATE = load_module("template_update", ROOT / "scripts/template_update.py")
BUILD = load_module(
    "build_template_manifest", ROOT / "scripts/build_template_manifest.py"
)
MIGRATION = load_module(
    "automatic_updates_migration",
    ROOT / "migrations/1.3.0_add_automatic_updates.py",
)
SOURCE_MIGRATION = load_module(
    "template_source_migration",
    ROOT / "migrations/2.0.0_rename_template_source.py",
)


class TemplateUpdateTests(unittest.TestCase):
    def test_default_policy_is_fully_automatic(self) -> None:
        workspace = json.loads(
            (ROOT / "workspace.example.json").read_text(encoding="utf-8")
        )
        updates = workspace["updates"]
        for key in ("autoCheck", "autoApply", "autoCommit", "autoPush"):
            self.assertIs(updates[key], True)
        self.assertEqual(updates["checkIntervalHours"], 24)
        self.assertIs(updates["rollbackOnFailure"], True)

    def test_manifest_allows_automatic_major_updates(self) -> None:
        manifest = json.loads(
            (ROOT / "template-manifest.json").read_text(encoding="utf-8")
        )
        # Сверяем manifest с каноническим VERSION, чтобы сам релизный тест не
        # требовал ручной правки при каждом корректном повышении версии.
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(manifest["version"], version)
        self.assertEqual(manifest["updateMode"], "automatic")
        self.assertIs(manifest["requiresUserAction"], False)

    def test_semver_and_repository_parsing(self) -> None:
        self.assertEqual(UPDATE.parse_version("v2.10.3"), (2, 10, 3))
        self.assertEqual(
            UPDATE.repository_coordinates(
                "https://github.com/ivaschru/personal-agent-workspace"
            ),
            ("ivaschru", "personal-agent-workspace"),
        )
        with self.assertRaises(ValueError):
            UPDATE.repository_coordinates("https://example.com/owner/repo")

    def test_three_way_decisions(self) -> None:
        self.assertEqual(UPDATE.decide_file_action(b"a", b"a", b"b"), "replace")
        self.assertEqual(UPDATE.decide_file_action(None, None, b"b"), "add")
        self.assertEqual(UPDATE.decide_file_action(b"a", b"a", None), "delete")
        self.assertEqual(UPDATE.decide_file_action(b"a", b"local", b"a"), "noop")
        self.assertEqual(UPDATE.decide_file_action(b"a", b"local", b"new"), "merge")

    def test_download_creates_archive_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "missing" / "base"
            extracted = destination / "v1.2.0" / "root"
            with (
                patch.object(
                    UPDATE.urllib.request,
                    "urlopen",
                    return_value=io.BytesIO(b"test archive"),
                ),
                patch.object(UPDATE, "safe_extract", return_value=extracted),
            ):
                result = UPDATE.download_tag(
                    "https://github.com/ivaschru/personal-agent-workspace",
                    "v1.2.0",
                    destination,
                )
            self.assertEqual(result, extracted)
            self.assertEqual((destination / "v1.2.0.tar.gz").read_bytes(), b"test archive")

    def test_workspace_guard_allows_only_service_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = root / "original.json"
            updated = root / "updated.json"
            before = {
                "owner": {"displayName": "Private"},
                "template": {"source": "central", "version": "1.2.0"},
            }
            after = {
                "owner": {"displayName": "Private"},
                "template": {"source": "central", "version": "1.3.0"},
                "updates": {"autoApply": True},
            }
            original.write_text(json.dumps(before), encoding="utf-8")
            updated.write_text(json.dumps(after), encoding="utf-8")
            UPDATE.validate_workspace_changes(original, updated)
            after["owner"]["displayName"] = "Changed"
            updated.write_text(json.dumps(after), encoding="utf-8")
            with self.assertRaises(ValueError):
                UPDATE.validate_workspace_changes(original, updated)

    def test_workspace_guard_allows_only_exact_source_rename(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = root / "original.json"
            updated = root / "updated.json"
            before = {
                "owner": {"displayName": "Private"},
                "template": {
                    "source": UPDATE.OLD_TEMPLATE_SOURCE,
                    "version": "1.6.0",
                },
            }
            after = {
                "owner": {"displayName": "Private"},
                "template": {
                    "source": UPDATE.NEW_TEMPLATE_SOURCE,
                    "version": "2.0.0",
                },
            }
            original.write_text(json.dumps(before), encoding="utf-8")
            updated.write_text(json.dumps(after), encoding="utf-8")
            UPDATE.validate_workspace_changes(original, updated)

            after["template"]["source"] = "https://github.com/example/untrusted"
            updated.write_text(json.dumps(after), encoding="utf-8")
            with self.assertRaises(ValueError):
                UPDATE.validate_workspace_changes(original, updated)

    def test_source_migration_is_idempotent_and_preserves_custom_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace.json"
            payload = {
                "owner": {"displayName": "Private"},
                "template": {"source": SOURCE_MIGRATION.OLD_SOURCE, "version": "1.6.0"},
            }
            workspace.write_text(json.dumps(payload), encoding="utf-8")
            self.assertTrue(SOURCE_MIGRATION.migrate(workspace))
            migrated = json.loads(workspace.read_text(encoding="utf-8"))
            self.assertEqual(migrated["template"]["source"], SOURCE_MIGRATION.NEW_SOURCE)
            self.assertFalse(SOURCE_MIGRATION.migrate(workspace))

            migrated["template"]["source"] = "https://github.com/example/custom"
            workspace.write_text(json.dumps(migrated), encoding="utf-8")
            self.assertFalse(SOURCE_MIGRATION.migrate(workspace))
            preserved = json.loads(workspace.read_text(encoding="utf-8"))
            self.assertEqual(preserved["template"]["source"], "https://github.com/example/custom")

    def test_workspace_guard_allows_only_initial_empty_modules(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = root / "original.json"
            updated = root / "updated.json"
            before = {
                "owner": {"displayName": "Private"},
                "template": {"source": "central", "version": "1.3.1"},
            }
            after = {
                **before,
                "template": {"source": "central", "version": "1.4.0"},
                "modules": [],
            }
            original.write_text(json.dumps(before), encoding="utf-8")
            updated.write_text(json.dumps(after), encoding="utf-8")
            UPDATE.validate_workspace_changes(original, updated)

            after["modules"] = [{"moduleId": "unexpected"}]
            updated.write_text(json.dumps(after), encoding="utf-8")
            with self.assertRaises(ValueError):
                UPDATE.validate_workspace_changes(original, updated)

    def test_manifest_excludes_personal_paths(self) -> None:
        manifest = json.loads(
            (ROOT / "template-manifest.json").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "skills/example").mkdir(parents=True)
            (root / "tasks/2026/0001--private").mkdir(parents=True)
            (root / "skills/example/SKILL.md").write_text("safe", encoding="utf-8")
            (root / "recovery-plan.json").write_text("private plan", encoding="utf-8")
            (root / "tasks/2026/0001--private/README.md").write_text(
                "private", encoding="utf-8"
            )
            files = BUILD.collect_files(root, manifest)
        self.assertIn("skills/example/SKILL.md", files)
        self.assertNotIn("recovery-plan.json", files)
        self.assertNotIn("tasks/2026/0001--private/README.md", files)

    def test_migration_preserves_existing_choices(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace.json"
            workspace.write_text(
                json.dumps(
                    {
                        "initialized": True,
                        "owner": {"displayName": "Test"},
                        "updates": {"autoPush": False},
                    }
                ),
                encoding="utf-8",
            )
            self.assertIs(MIGRATION.migrate(workspace), True)
            migrated = json.loads(workspace.read_text(encoding="utf-8"))
            self.assertEqual(migrated["owner"], {"displayName": "Test"})
            self.assertIs(migrated["updates"]["autoPush"], False)
            self.assertIs(migrated["updates"]["autoApply"], True)
            self.assertIs(MIGRATION.migrate(workspace), False)


if __name__ == "__main__":
    unittest.main()
