#!/usr/bin/env python3
"""Идемпотентно переводит canonical source на Personal Agent Workspace."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = ROOT / "workspace.json"
OLD_SOURCE = "https://github.com/ivaschru/personal-codex-workspace"
NEW_SOURCE = "https://github.com/ivaschru/personal-agent-workspace"


def migrate(path: Path = WORKSPACE) -> bool:
    """Меняет только прежний официальный URL, не трогая сторонний upstream."""

    if not path.exists():
        raise FileNotFoundError("workspace.json отсутствует")
    data = json.loads(path.read_text(encoding="utf-8"))
    template = data.setdefault("template", {})
    if template.get("source") != OLD_SOURCE:
        return False
    template["source"] = NEW_SOURCE
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


if __name__ == "__main__":
    changed = migrate()
    print("Источник шаблона обновлён." if changed else "Источник шаблона уже актуален или настроен отдельно.")
