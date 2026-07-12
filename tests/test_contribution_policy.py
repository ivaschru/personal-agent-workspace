"""Регрессии безопасной политики обратного вклада в шаблон."""

from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ContributionPolicyTests(unittest.TestCase):
    def test_draft_pull_requests_are_enabled_by_default(self) -> None:
        workspace = json.loads(
            (ROOT / "workspace.example.json").read_text(encoding="utf-8")
        )
        policy = workspace["contributions"]
        self.assertIs(policy["allowDraftPullRequests"], True)
        self.assertIs(policy["allowPublicIssues"], False)
        self.assertIs(policy["requireCleanReproduction"], True)
        self.assertIs(policy["requirePrivacyScan"], True)
        self.assertIs(policy["securityReportsPrivate"], True)

    def test_template_source_and_version_are_pinned(self) -> None:
        workspace = json.loads(
            (ROOT / "workspace.example.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            workspace["template"]["source"],
            "https://github.com/ivaschru/personal-codex-workspace",
        )
        self.assertEqual(
            workspace["template"]["version"],
            (ROOT / "VERSION").read_text(encoding="utf-8").strip(),
        )

    def test_skill_requires_clean_copy_and_selects_pr_status_by_authority(self) -> None:
        skill = (ROOT / "skills/contribute-template-fix/SKILL.md").read_text(
            encoding="utf-8"
        )
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")

        self.assertIn("чистую копию центрального репозитория", skill)
        self.assertIn("Не использовать личный private repository", skill)
        self.assertIn("Security Advisory", skill)

        # Автономная публикация остаётся черновиком, а прямое поручение владельца
        # разрешает сразу показать полностью проверенный результат как ready.
        for text in (skill, agents, contributing):
            self.assertIn("allowDraftPullRequests=true", text)
            self.assertIn("прямо поручил", text)
            self.assertIn("ready PR", text)
            self.assertIn("draft PR", text)

        # Готовность PR не расширяет полномочия до необратимых действий.
        self.assertIn("Не делать merge", skill)
        self.assertIn("- делать merge;", contributing)


if __name__ == "__main__":
    unittest.main()
