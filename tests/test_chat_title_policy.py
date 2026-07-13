"""Регрессия переносимой политики названий разговоров с агентами."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ChatTitlePolicyTests(unittest.TestCase):
    def test_policy_names_current_thread_without_guessing(self) -> None:
        """Защищает и полезное переименование, и границу от выбора чужого треда."""

        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("## Агентские чаты", agents)
        self.assertIn("одноразовой нормализацией", agents)
        self.assertIn("без ручного выбора идентификатора", agents)
        self.assertIn("проверяй одновременно несколько признаков", agents)
        self.assertIn("не угадывай идентификатор", agents)
        self.assertIn("среда не поддерживает названия разговоров", agents)

    def test_policy_keeps_service_identifiers_out_of_titles(self) -> None:
        """Не даёт будущим правкам превратить заголовки в технические ярлыки."""

        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("Не добавляй в заголовок номера внутренних задач", agents)
        self.assertIn("идентификаторы разговоров или тредов", agents)
        self.assertIn("самый короткий читаемый вариант", agents)


if __name__ == "__main__":
    unittest.main()
