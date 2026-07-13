#!/usr/bin/env python3
"""Проверяет публичный шаблон или уже настроенное личное пространство.

Скрипт использует только стандартную библиотеку, чтобы первичная проверка
работала до установки каких-либо зависимостей. Он намеренно не пытается
угадывать безопасность содержимого: вместо этого проверяет минимальные
инварианты и несколько распространённых признаков случайной публикации.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
REQUIRED_TEMPLATE_PATHS = (
    "AGENTS.md",
    "CLAUDE.md",
    "BOOTSTRAP_UPDATE.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "INTEGRATIONS.md",
    "SETUP.md",
    "VERSION",
    "PROFILE.example.md",
    "RELEASES.md",
    "scripts/build_template_manifest.py",
    "scripts/template_update.py",
    "scripts/workspace_modules.py",
    "workspace.example.json",
    "templates/task/README.md",
    "templates/task/external-assets.yml",
    "templates/project/README.md",
    "templates/project/external-assets.yml",
    "skills/create-private-workspace/SKILL.md",
    "skills/accept-workspace-share/SKILL.md",
    "skills/contribute-template-fix/SKILL.md",
    "skills/email-mailbox/SKILL.md",
    "skills/external-file-storage/SKILL.md",
    "skills/encrypted-recovery/SKILL.md",
    "skills/encrypted-recovery/scripts/encrypted_recovery.py",
    "skills/gas-pravosudie/SKILL.md",
    "skills/gosuslugi/SKILL.md",
    "skills/setup-workspace/SKILL.md",
    "skills/max-messenger/SKILL.md",
    "skills/manage-personal-task/SKILL.md",
    "skills/manage-personal-project/SKILL.md",
    "skills/ozon-buyer-search/SKILL.md",
    "skills/process-incoming-file/SKILL.md",
    "skills/russian-post-registered-mail/SKILL.md",
    "skills/share-workspace-object/SKILL.md",
    "skills/t-bank/SKILL.md",
    "skills/telegram-messenger/SKILL.md",
    "skills/trelio/SKILL.md",
    "skills/update-workspace-template/SKILL.md",
    "template-manifest.json",
)
TEXT_SUFFIXES = {".md", ".json", ".yml", ".yaml", ".py", ".txt"}
SUSPICIOUS_PATTERNS = {
    "абсолютный пользовательский путь": re.compile(r"/(?:Users|home)/[^/\s]+/"),
    "похожий на GitHub-токен фрагмент": re.compile(r"\bgh[opsu]_[A-Za-z0-9]{20,}\b"),
    "начало приватного ключа": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}


def fail(message: str, errors: list[str]) -> None:
    """Добавляет диагностическую ошибку, не прерывая остальные проверки."""

    errors.append(message)


def read_json(path: Path, errors: list[str]) -> dict:
    """Читает JSON и возвращает пустой объект после понятной ошибки формата."""

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"Не удалось прочитать {path.relative_to(ROOT)}: {exc}", errors)
        return {}


def check_required_paths(errors: list[str]) -> None:
    for relative in REQUIRED_TEMPLATE_PATHS:
        if not (ROOT / relative).exists():
            fail(f"Отсутствует обязательный путь: {relative}", errors)


def check_template(errors: list[str]) -> None:
    """Проверяет, что публичный исходник остаётся обезличенным шаблоном."""

    for forbidden in ("PROFILE.md", "workspace.json", ".local"):
        if (ROOT / forbidden).exists():
            fail(f"В публичном шаблоне не должно быть: {forbidden}", errors)

    claude_path = ROOT / "CLAUDE.md"
    if claude_path.exists() and "@AGENTS.md" not in claude_path.read_text(encoding="utf-8"):
        fail("CLAUDE.md должен импортировать канонический AGENTS.md", errors)

    example = read_json(ROOT / "workspace.example.json", errors)
    if example and example.get("initialized") is not False:
        fail("workspace.example.json должен содержать initialized=false", errors)

    version_path = ROOT / "VERSION"
    if version_path.exists():
        version = version_path.read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"\d+\.\d+\.\d+", version):
            fail("VERSION должен содержать SemVer вида X.Y.Z", errors)
        if example and example.get("template", {}).get("version") != version:
            fail("workspace.example.json.template.version должен совпадать с VERSION", errors)

    contributions = example.get("contributions", {}) if example else {}
    if contributions.get("allowDraftPullRequests") is not True:
        fail("Публичный шаблон должен включать draft PR policy по умолчанию", errors)
    if contributions.get("allowPublicIssues") is not False:
        fail("Публичные issues не должны быть разрешены по умолчанию", errors)
    if contributions.get("securityReportsPrivate") is not True:
        fail("Security reports должны оставаться приватными", errors)

    updates = example.get("updates", {}) if example else {}
    for key in ("autoCheck", "autoApply", "autoCommit", "autoPush", "rollbackOnFailure"):
        if updates.get(key) is not True:
            fail(f"Update policy должна включать {key}=true по умолчанию", errors)

    if example.get("modules") != []:
        fail("workspace.example.json должен начинаться с пустого массива modules", errors)

    manifest_path = ROOT / "template-manifest.json"
    if manifest_path.exists():
        manifest = read_json(manifest_path, errors)
        if version_path.exists() and manifest.get("version") != version:
            fail("template-manifest.json.version должен совпадать с VERSION", errors)
        if manifest.get("updateMode") != "automatic":
            fail("Release manifest должен использовать automatic updateMode", errors)
        if manifest.get("requiresUserAction") is not False:
            fail("Release manifest не должен требовать ручного действия по умолчанию", errors)

    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts or path.suffix not in TEXT_SUFFIXES:
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        for label, pattern in SUSPICIOUS_PATTERNS.items():
            if pattern.search(content):
                fail(f"{path.relative_to(ROOT)}: найден {label}", errors)


def check_installed(errors: list[str]) -> None:
    """Проверяет переносимую конфигурацию без machine-local состояния."""

    workspace = ROOT / "workspace.json"
    if not workspace.exists():
        fail("После настройки отсутствует: workspace.json", errors)
        return

    data = read_json(workspace, errors)
    if data and data.get("initialized") is not True:
        fail("workspace.json должен содержать initialized=true", errors)
    visibility = data.get("privacy", {}).get("repositoryVisibility")
    if visibility not in {"private", "local-only"}:
        fail("Видимость должна быть подтверждена как private или local-only", errors)
    if not isinstance(data.get("modules"), list):
        fail("workspace.json.modules должен быть массивом", errors)

    storage = data.get("storage", {})
    if isinstance(storage, dict) and storage.get("backup") == "encrypted-recovery":
        plan_name = storage.get("recoveryPlan")
        if plan_name != "recovery-plan.json":
            fail(
                "encrypted-recovery должен ссылаться на recovery-plan.json",
                errors,
            )
        elif not (ROOT / plan_name).is_file():
            fail("Для encrypted-recovery отсутствует recovery-plan.json", errors)

    version_path = ROOT / "VERSION"
    if version_path.exists() and data:
        installed_version = data.get("template", {}).get("version")
        if installed_version != version_path.read_text(encoding="utf-8").strip():
            fail("workspace.json.template.version должен совпадать с VERSION", errors)


def check_configured(errors: list[str]) -> None:
    """Проверяет персональную конфигурацию вместе с текущим компьютером."""

    check_installed(errors)
    profile = ROOT / "PROFILE.md"
    machine = ROOT / ".local/machine-setup.json"

    for path in (profile, machine):
        if not path.exists():
            fail(f"После настройки отсутствует: {path.relative_to(ROOT)}", errors)

    if machine.exists():
        read_json(machine, errors)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", choices=("template", "installed", "configured"), required=True
    )
    args = parser.parse_args()

    errors: list[str] = []
    check_required_paths(errors)
    if args.mode == "template":
        check_template(errors)
    elif args.mode == "installed":
        check_installed(errors)
    else:
        check_configured(errors)

    if errors:
        print("Проверка не пройдена:")
        for error in errors:
            print(f"- {error}")
        return 1

    print(f"Проверка режима {args.mode} пройдена.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
