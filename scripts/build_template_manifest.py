#!/usr/bin/env python3
"""Строит и проверяет SHA-256 реестр управляемых файлов шаблона."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = ROOT / "template-manifest.json"


def matches(path: str, patterns: list[str]) -> bool:
    """Сопоставляет POSIX-путь с декларативными glob-паттернами manifest."""

    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def collect_files(root: Path, manifest: dict) -> dict[str, str]:
    """Возвращает checksums только переносимых управляемых файлов."""

    managed = list(manifest.get("managedPatterns", []))
    protected = list(manifest.get("protectedPatterns", []))
    checksums: dict[str, str] = {}

    for path in sorted(root.rglob("*")):
        if (
            not path.is_file()
            or ".git" in path.parts
            or "__pycache__" in path.parts
            or path.suffix in {".pyc", ".pyo"}
        ):
            continue
        relative = path.relative_to(root).as_posix()
        # Manifest нельзя включить в собственный hash, иначе любое обновление
        # поля files меняло бы проверяемое значение рекурсивно.
        if relative == "template-manifest.json":
            continue
        if not matches(relative, managed) or matches(relative, protected):
            continue
        checksums[relative] = hashlib.sha256(path.read_bytes()).hexdigest()

    return checksums


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args()

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    expected = collect_files(ROOT, manifest)

    if args.check:
        actual = manifest.get("files", {})
        if actual != expected:
            missing = sorted(set(expected) - set(actual))
            stale = sorted(set(actual) - set(expected))
            changed = sorted(
                path for path in set(actual) & set(expected) if actual[path] != expected[path]
            )
            print("Manifest устарел.")
            if missing:
                print(f"Не добавлены: {', '.join(missing)}")
            if stale:
                print(f"Лишние: {', '.join(stale)}")
            if changed:
                print(f"Изменились: {', '.join(changed)}")
            return 1
        print(f"Manifest проверен: {len(expected)} файлов.")
        return 0

    manifest["files"] = expected
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Manifest обновлён: {len(expected)} файлов.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
