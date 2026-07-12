#!/usr/bin/env python3
"""Проверяет включение переносимых сервисных навыков без доступа к секретам.

Preflight намеренно не открывает браузер и не проверяет аккаунт. Его задача –
отделить конфигурацию репозитория от ручной авторизации в конкретном сервисе.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INTEGRATIONS = {
    "email-mailbox": "электронная почта: API провайдера, IMAP для чтения и SMTP для отправки",
    "gas-pravosudie": "ГАС Правосудие через ЕСИА; подача и подпись остаются ручными",
    "gosuslugi": "Госуслуги и ЕСИА; проверки личности остаются ручными",
    "max-messenger": "веб-МАКС; отправка только по явной команде",
    "telegram-messenger": "Telegram; отправка только по явной команде",
    "ozon-buyer-search": "покупательский поиск и чтение выбранного заказа",
    "russian-post-registered-mail": "электронное заказное письмо; оплата и отправка подтверждаются",
    "t-bank": "только чтение личного интернет-банка; любые изменения запрещены",
    "trelio": "MCP-only задачи и проекты; изменения только по явной команде",
}


def load_workspace(root: Path) -> dict | None:
    path = root / "workspace.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Некорректный workspace.json: {exc}") from exc


def enabled_integrations(workspace: dict) -> set[str]:
    value = workspace.get("features", {}).get("externalIntegrations", {})
    if isinstance(value, list):
        return {str(item) for item in value}
    if isinstance(value, dict):
        enabled = value.get("enabled", [])
        if isinstance(enabled, list):
            return {str(item) for item in enabled}
    return set()


def list_integrations() -> None:
    for name, description in INTEGRATIONS.items():
        print(f"{name}: {description}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("integration", nargs="?", choices=sorted(INTEGRATIONS))
    parser.add_argument("--list", action="store_true", dest="show_list")
    args = parser.parse_args()

    if args.show_list:
        list_integrations()
        return 0
    if not args.integration:
        parser.error("укажите интеграцию или --list")

    try:
        workspace = load_workspace(ROOT)
    except ValueError as exc:
        print(exc)
        return 2

    if not workspace or workspace.get("initialized") is not True:
        print("Сначала завершите первичную настройку через $setup-workspace.")
        return 2

    system = platform.system() or "Unknown"
    print(f"Интеграция: {args.integration}")
    print(f"Платформа: {system}")
    print(f"Режим: {INTEGRATIONS[args.integration]}")

    if args.integration == "email-mailbox":
        config = ROOT / ".local" / "email" / "accounts.toml"
        if config.exists():
            print("Локальная конфигурация: найдена; значения доступов не проверялись.")
        else:
            print(
                "Локальная конфигурация: отсутствует; используйте "
                "skills/email-mailbox/references/accounts.example.toml."
            )

    if args.integration == "t-bank":
        if system == "Darwin":
            print("Защита профиля: рекомендуется FileVault и macOS Keychain.")
        elif system == "Windows":
            print("Защита профиля: рекомендуется BitLocker и Windows Credential Manager.")
        else:
            print("Защищённый постоянный профиль не заявлен; используйте ручную сессию.")

    enabled = enabled_integrations(workspace)
    if args.integration not in enabled:
        print("Статус: доступна, но не включена владельцем.")
        return 3

    print("Статус: конфигурация включена; продолжите через соответствующий навык.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
