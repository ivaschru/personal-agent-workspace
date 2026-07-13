from __future__ import annotations

from argparse import Namespace
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import tarfile
import unittest
from unittest import mock


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills/encrypted-recovery/scripts/encrypted_recovery.py"
)
SPEC = importlib.util.spec_from_file_location("encrypted_recovery", SCRIPT)
assert SPEC and SPEC.loader
RECOVERY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RECOVERY
SPEC.loader.exec_module(RECOVERY)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class EncryptedRecoveryTests(unittest.TestCase):
    @contextmanager
    def configured_workspace(self):
        """Создаёт изолированный Workspace и заменяет crypto на test double.

        Fake-age копирует bytes без шифрования. Это намеренно: unit-тесты
        проверяют orchestration, allowlist, manifest и восстановление, а реальный
        `age` отдельно проверяется локальным интеграционным smoke test.
        """

        with tempfile.TemporaryDirectory() as temporary_name:
            base = Path(temporary_name)
            root = base / "workspace"
            storage = base / "storage"
            root.mkdir()
            storage.mkdir()
            (root / ".local/email").mkdir(parents=True)
            (root / ".local/email/token.txt").write_text(
                "test-secret", encoding="utf-8"
            )
            write_json(
                root / "workspace.json",
                {
                    "initialized": True,
                    "privacy": {"repositoryVisibility": "private"},
                    "storage": {"backup": "encrypted-recovery"},
                },
            )
            write_json(
                root / ".local/external-file-storage.json",
                {"schemaVersion": 1, "root": str(storage)},
            )
            write_json(
                root / "recovery-plan.json",
                {
                    "schemaVersion": 1,
                    "encryption": "age-passphrase",
                    "storageSubdir": "recovery",
                    "retention": {
                        "keepAllDays": 30,
                        "keepWeeklyDays": 90,
                        "keepMonthlyDays": 365,
                    },
                    "archives": [
                        {
                            "id": "workspace-secrets",
                            "description": "test",
                            "sources": [".local/email/token.txt"],
                            "required": True,
                        }
                    ],
                },
            )

            fake_age = base / "age"
            fake_age.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, shutil, sys\n"
                "destination = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n"
                "source = pathlib.Path(sys.argv[-1])\n"
                "shutil.copyfile(source, destination)\n",
                encoding="utf-8",
            )
            fake_age.chmod(0o755)
            template = base / "recovery-plan.example.json"
            template.write_text(
                (Path(__file__).resolve().parents[1]
                 / "skills/encrypted-recovery/assets/recovery-plan.example.json")
                .read_text(encoding="utf-8"),
                encoding="utf-8",
            )

            with (
                mock.patch.multiple(
                    RECOVERY,
                    ROOT=root,
                    PLAN_PATH=root / "recovery-plan.json",
                    PLAN_TEMPLATE=template,
                    EXTERNAL_STORAGE_CONFIG=root
                    / ".local/external-file-storage.json",
                    WORK_DIRECTORY=root / ".local/recovery-work",
                    DEFAULT_RESTORE_ROOT=root / ".local/recovery-restore",
                ),
                mock.patch.dict(
                    os.environ,
                    {"PATH": f"{base}{os.pathsep}{os.environ.get('PATH', '')}"},
                    clear=False,
                ),
            ):
                yield root, storage

    def test_backup_verify_and_restore_round_trip(self) -> None:
        with self.configured_workspace() as (root, storage):
            plan = RECOVERY.load_plan()
            archive = plan.archives[0]
            directory = RECOVERY.storage_directory(plan)
            created = RECOVERY.backup_one(
                archive,
                directory,
            )

            self.assertTrue(created.is_file())
            latest = storage / "recovery/workspace-secrets-latest.tar.age"
            self.assertTrue(latest.is_file())

            with tempfile.TemporaryDirectory(dir=root / ".local") as temporary:
                plain = Path(temporary) / "verified.tar.gz"
                manifest = RECOVERY.decrypt_and_verify(
                    latest,
                    plain,
                    archive_id="workspace-secrets",
                )
                self.assertEqual(
                    [".local/email/token.txt"],
                    [item["path"] for item in manifest["files"]],
                )
                output = root / ".local/recovery-restore/test"
                RECOVERY.extract_verified_archive(plain, manifest, output)

            restored = output / ".local/email/token.txt"
            self.assertEqual("test-secret", restored.read_text(encoding="utf-8"))
            if os.name == "posix":
                self.assertEqual(0o600, restored.stat().st_mode & 0o777)

    def test_plan_rejects_whole_local_directory_and_external_paths(self) -> None:
        with self.configured_workspace() as (root, _storage):
            raw = json.loads((root / "recovery-plan.json").read_text(encoding="utf-8"))
            for unsafe in (".local", "PROFILE.md", "../outside"):
                with self.subTest(unsafe=unsafe):
                    raw["archives"][0]["sources"] = [unsafe]
                    write_json(root / "recovery-plan.json", raw)
                    with self.assertRaises(RECOVERY.RecoveryError):
                        RECOVERY.load_plan()

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink is unavailable")
    def test_intermediate_symlink_cannot_escape_local_directory(self) -> None:
        with self.configured_workspace() as (root, _storage):
            outside = root.parent / "outside"
            outside.mkdir()
            (outside / "secret.txt").write_text("outside", encoding="utf-8")
            os.symlink(outside, root / ".local/link")
            with self.assertRaises(RECOVERY.RecoveryError):
                RECOVERY.validate_source_location(".local/link/secret.txt")

    def test_restore_refuses_to_overwrite_existing_directory(self) -> None:
        with self.configured_workspace() as (root, _storage):
            output = root / ".local/already-there"
            output.mkdir()
            with self.assertRaises(RECOVERY.RecoveryError):
                RECOVERY.extract_verified_archive(
                    root / "does-not-matter.tar.gz",
                    {"files": []},
                    output,
                )

    def test_manifest_hash_detects_modified_payload(self) -> None:
        with self.configured_workspace() as (root, _storage):
            archive_plan = RECOVERY.load_plan().archives[0]
            original = root / ".local/original.tar.gz"
            tampered = root / ".local/tampered.tar.gz"
            RECOVERY.build_plain_archive(archive_plan, original)

            with tarfile.open(original, "r:gz") as source:
                manifest = source.extractfile(RECOVERY.MANIFEST_NAME)
                assert manifest is not None
                manifest_bytes = manifest.read()
            with tarfile.open(tampered, "w:gz") as target:
                manifest_info = tarfile.TarInfo(RECOVERY.MANIFEST_NAME)
                manifest_info.size = len(manifest_bytes)
                target.addfile(manifest_info, io.BytesIO(manifest_bytes))
                payload = b"modified-secret"
                payload_info = tarfile.TarInfo("files/.local/email/token.txt")
                payload_info.size = len(payload)
                target.addfile(payload_info, io.BytesIO(payload))

            with self.assertRaises(RECOVERY.RecoveryError):
                RECOVERY.verify_plain_archive(
                    tampered, expected_id="workspace-secrets"
                )

    def test_retention_keeps_fresh_and_latest_weekly_monthly_points(self) -> None:
        now = datetime(2026, 7, 13, tzinfo=timezone.utc)
        policy = RECOVERY.RetentionPolicy(30, 90, 365)

        def named(days: int, suffix: str = "") -> Path:
            created = now - timedelta(days=days)
            stamp = created.strftime("%Y%m%dT%H%M%SZ")
            return Path(f"scope-{stamp}{suffix}.tar.age")

        fresh = named(2)
        weekly_old = named(45)
        weekly_new = named(44)
        monthly_old = named(150)
        monthly_new = named(149)
        expired = named(500)
        unknown = Path("scope-manual.tar.age")
        keep = RECOVERY.retention_keep_set(
            [
                fresh,
                weekly_old,
                weekly_new,
                monthly_old,
                monthly_new,
                expired,
                unknown,
            ],
            policy,
            now=now,
        )

        self.assertIn(fresh, keep)
        self.assertIn(weekly_new, keep)
        self.assertNotIn(weekly_old, keep)
        self.assertIn(monthly_new, keep)
        self.assertNotIn(monthly_old, keep)
        self.assertNotIn(expired, keep)
        self.assertIn(unknown, keep)

    def test_prune_requires_explicit_sync_confirmation(self) -> None:
        with self.configured_workspace() as (_root, storage):
            directory = storage / "recovery"
            directory.mkdir()
            expired_time = datetime.now(timezone.utc) - timedelta(days=500)
            expired = directory / (
                "workspace-secrets-"
                + expired_time.strftime("%Y%m%dT%H%M%SZ")
                + ".tar.age"
            )
            expired.write_bytes(b"old encrypted archive")

            RECOVERY.command_prune(
                Namespace(archive=None, confirm_synced=False)
            )
            self.assertTrue(expired.exists())

            RECOVERY.command_prune(
                Namespace(archive=None, confirm_synced=True)
            )
            self.assertFalse(expired.exists())

    def test_init_creates_empty_plan_and_updates_workspace(self) -> None:
        with self.configured_workspace() as (root, _storage):
            (root / "recovery-plan.json").unlink()
            RECOVERY.command_init(Namespace(force=False))
            plan = RECOVERY.load_plan()
            self.assertEqual((), plan.archives[0].sources)
            workspace = json.loads(
                (root / "workspace.json").read_text(encoding="utf-8")
            )
            self.assertEqual("encrypted-recovery", workspace["storage"]["backup"])
            self.assertEqual("recovery-plan.json", workspace["storage"]["recoveryPlan"])


if __name__ == "__main__":
    unittest.main()
