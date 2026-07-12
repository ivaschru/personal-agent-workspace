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
        self.assertEqual(workspace["template"]["version"], "1.2.0")

    def test_skill_requires_clean_copy_and_draft_pr(self) -> None:
        text = (ROOT / "skills/contribute-template-fix/SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("чистую копию центрального репозитория", text)
        self.assertIn("draft PR", text)
        self.assertIn("Security Advisory", text)
        self.assertIn("Не использовать личный private repository", text)


if __name__ == "__main__":
    unittest.main()
