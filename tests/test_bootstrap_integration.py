"""Локальный end-to-end bootstrap 1.2.0 -> текущая версия.

GitHub Actions обычно получает shallow checkout без старых tags, поэтому тест
прозрачно пропускается там. В полном maintainer checkout он проверяет реальное
трёхстороннее обновление и сохранение личных файлов.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_update_module():
    path = ROOT / "scripts/template_update.py"
    spec = importlib.util.spec_from_file_location("bootstrap_template_update", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


UPDATE = load_update_module()


def tag_available(tag: str) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", tag],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


@unittest.skipUnless(tag_available("v1.2.0"), "v1.2.0 tag отсутствует в checkout")
class BootstrapIntegrationTests(unittest.TestCase):
    def test_bootstrap_preserves_personal_data(self) -> None:
        archive = subprocess.run(
            ["git", "archive", "--format=tar", "v1.2.0"],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            check=True,
        ).stdout

        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            base = temp_root / "base"
            private = temp_root / "private"
            base.mkdir()
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
                bundle.extractall(base)
            shutil.copytree(base, private)

            profile_text = "# Личный тестовый профиль\nНе изменять.\n"
            (private / "PROFILE.md").write_text(profile_text, encoding="utf-8")
            personal_task = private / "tasks/2026/0001--private/README.md"
            personal_task.parent.mkdir(parents=True)
            personal_task.write_text("Личная задача\n", encoding="utf-8")

            workspace = json.loads(
                (base / "workspace.example.json").read_text(encoding="utf-8")
            )
            workspace["initialized"] = True
            workspace["owner"]["displayName"] = "Private Owner"
            workspace["privacy"]["repositoryVisibility"] = "private"
            (private / "workspace.json").write_text(
                json.dumps(workspace, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            subprocess.run(["git", "init", "-b", "main"], cwd=private, check=True)
            subprocess.run(
                ["git", "config", "user.name", "Updater Test"], cwd=private, check=True
            )
            subprocess.run(
                ["git", "config", "user.email", "updater-test@example.invalid"],
                cwd=private,
                check=True,
            )
            subprocess.run(["git", "add", "--all"], cwd=private, check=True)
            subprocess.run(
                ["git", "commit", "-m", "Create private workspace"],
                cwd=private,
                stdout=subprocess.DEVNULL,
                check=True,
            )

            manifest = json.loads(
                (ROOT / "template-manifest.json").read_text(encoding="utf-8")
            )
            with (
                patch.object(UPDATE, "ROOT", private),
                patch.object(UPDATE, "WORKSPACE_PATH", private / "workspace.json"),
                patch.object(UPDATE, "MANIFEST_PATH", private / "template-manifest.json"),
                patch.object(UPDATE, "STATE_PATH", private / ".local/template-update-state.json"),
            ):
                conflicts = UPDATE.apply_files(base, ROOT, manifest)
                self.assertEqual(conflicts, [])

            # Финализацию запускаем именно внешним updater нового release. Это
            # воспроизводит bootstrap копии, где собственного updater ещё нет.
            environment = os.environ.copy()
            environment["PERSONAL_CODEX_WORKSPACE_ROOT"] = str(private)
            finalized = subprocess.run(
                [sys.executable, str(ROOT / "scripts/template_update.py"), "--finalize"],
                cwd=private,
                env=environment,
                check=False,
            )
            self.assertEqual(finalized.returncode, 0)

            migrated = json.loads(
                (private / "workspace.json").read_text(encoding="utf-8")
            )
            # Bootstrap обязан записать версию проверяемого release, а не
            # застывшее значение конкретного выпуска из истории теста.
            release_version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
            self.assertEqual(migrated["template"]["version"], release_version)
            self.assertEqual(migrated["owner"]["displayName"], "Private Owner")
            self.assertIs(migrated["updates"]["autoApply"], True)
            self.assertEqual(migrated["modules"], [])
            self.assertEqual((private / "PROFILE.md").read_text(encoding="utf-8"), profile_text)
            self.assertEqual(personal_task.read_text(encoding="utf-8"), "Личная задача\n")


if __name__ == "__main__":
    unittest.main()
