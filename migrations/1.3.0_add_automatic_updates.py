#!/usr/bin/env python3
"""Идемпотентно добавляет default automatic-update policy в workspace.json."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = ROOT / "workspace.json"
DEFAULTS = {
    "channel": "stable",
    "autoCheck": True,
    "checkIntervalHours": 24,
    "autoApply": True,
    "autoCommit": True,
    "autoPush": True,
    "rollbackOnFailure": True,
    "stopOnlyOnUnsafeState": True,
}


def migrate(path: Path = WORKSPACE) -> bool:
    """Добавляет только отсутствующие ключи и возвращает факт изменения."""

    if not path.exists():
        raise FileNotFoundError("workspace.json отсутствует")
    data = json.loads(path.read_text(encoding="utf-8"))
    updates = data.setdefault("updates", {})
    changed = False
    for key, value in DEFAULTS.items():
        if key not in updates:
            updates[key] = value
            changed = True
    if changed:
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return changed


if __name__ == "__main__":
    changed = migrate()
    print("Update policy добавлена." if changed else "Update policy уже настроена.")
