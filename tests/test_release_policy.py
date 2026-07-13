"""Проверяет единый формат и версионные инварианты релизов."""

from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReleasePolicyTests(unittest.TestCase):
    def test_tracked_version_references_are_consistent(self) -> None:
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        workspace = json.loads(
            (ROOT / "workspace.example.json").read_text(encoding="utf-8")
        )
        manifest = json.loads(
            (ROOT / "template-manifest.json").read_text(encoding="utf-8")
        )
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertEqual(workspace["template"]["version"], version)
        self.assertEqual(manifest["version"], version)
        self.assertIn(f"Текущая версия: **{version}**.", readme)

    def test_release_template_defines_required_user_sections(self) -> None:
        policy = (ROOT / "RELEASES.md").read_text(encoding="utf-8")
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

        # Фиксированные секции дают владельцу одинаковую навигацию в каждом
        # выпуске; `Важно` остаётся опциональной и не проверяется как обязательная.
        self.assertIn("## Что нового", policy)
        self.assertIn("## Обновление", policy)
        self.assertIn("Название релиза – точная версия без префикса", policy)
        self.assertIn("Секцию `Проверки` в GitHub Release не добавлять", policy)
        self.assertIn("следуй `RELEASES.md`", agents)


if __name__ == "__main__":
    unittest.main()
