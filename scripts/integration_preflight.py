#!/usr/bin/env python3
"""Проверяет включение переносимых сервисных навыков без доступа к секретам.

Preflight намеренно не открывает браузер и не проверяет аккаунт. Его задача –
отделить конфигурацию репозитория от ручной авторизации в конкретном сервисе.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import platform
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INTEGRATIONS = {
    "email-mailbox": "электронная почта: API провайдера, IMAP для чтения и SMTP для отправки",
    "external-file-storage": "локальная папка внешнего или синхронизируемого файлового хранилища",
    "gas-pravosudie": "ГАС Правосудие через ЕСИА; подача и подпись остаются ручными",
    "gosuslugi": "Госуслуги и ЕСИА; проверки личности остаются ручными",
    "max-messenger": "веб-МАКС; отправка только по явной команде",
    "telegram-messenger": "Telegram; отправка только по явной команде",
    "ozon-buyer-search": "покупательский поиск и чтение выбранного заказа",
    "russian-post-registered-mail": "электронное заказное письмо; оплата и отправка подтверждаются",
    "t-bank": "только чтение личного интернет-банка; любые изменения запрещены",
    "trelio": "MCP-only задачи и проекты; изменения только по явной команде",
}
EXTERNAL_STORAGE_CONFIG = Path(".local/external-file-storage.json")


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


def enable_integration(workspace: dict, integration: str) -> None:
    """Включает интеграцию, сохраняя современную объектную схему настройки.

    Старые личные копии могли хранить список прямо в `externalIntegrations`.
    При явном `--setup` безопасно мигрируем только это поле и не затрагиваем
    остальные пользовательские возможности Workspace.
    """

    features = workspace.setdefault("features", {})
    current = features.get("externalIntegrations", {})
    if isinstance(current, list):
        settings = {"setupMode": "on-demand", "enabled": list(current)}
    elif isinstance(current, dict):
        settings = dict(current)
    else:
        settings = {"setupMode": "on-demand", "enabled": []}

    enabled = settings.get("enabled", [])
    if not isinstance(enabled, list):
        enabled = []
    settings["enabled"] = sorted({str(item) for item in enabled} | {integration})
    settings.setdefault("setupMode", "on-demand")
    features["externalIntegrations"] = settings


def external_storage_config_path(root: Path) -> Path:
    """Возвращает machine-local путь конфигурации выбранной папки."""

    return root / EXTERNAL_STORAGE_CONFIG


def load_external_storage(root: Path) -> Path | None:
    """Читает выбранный локальный путь, не проверяя содержимое хранилища."""

    config_path = external_storage_config_path(root)
    if not config_path.exists():
        return None
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    selected = data.get("root") if isinstance(data, dict) else None
    return Path(selected).expanduser() if isinstance(selected, str) and selected else None


def storage_status(selected: Path | None) -> tuple[bool, str]:
    """Проверяет доступность без создания файлов во время обычного preflight.

    `os.access` является предварительной проверкой, а не гарантией будущей
    записи. Поэтому реальный read/write/delete выполняется только во время
    явной настройки, когда пользователь ожидает локальную мутацию.
    """

    if selected is None:
        return False, "локальная папка ещё не выбрана"
    if not selected.exists():
        return False, f"папка не найдена: {selected}"
    if not selected.is_dir():
        return False, f"выбранный путь не является папкой: {selected}"
    if not os.access(selected, os.R_OK | os.W_OK | os.X_OK):
        return False, f"недостаточно прав чтения и записи: {selected}"
    return True, f"папка доступна: {selected}"


def verify_storage_write(selected: Path) -> None:
    """Проверяет запись, чтение и удаление уникального временного файла."""

    marker = b"personal-agent-workspace external storage preflight\n"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".workspace-storage-preflight-",
            dir=selected,
            delete=False,
        ) as handle:
            handle.write(marker)
            temporary_path = Path(handle.name)
        if temporary_path.read_bytes() != marker:
            raise OSError("содержимое проверочного файла изменилось после записи")
    finally:
        # Даже при ошибке чтения не оставляем служебный файл в папке владельца.
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def write_json_atomic(path: Path, data: dict, *, private: bool = False) -> None:
    """Атомарно записывает JSON и при необходимости ограничивает права POSIX."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if private and os.name == "posix":
        temporary.chmod(0o600)
    os.replace(temporary, path)


def setup_external_storage(root: Path, workspace: dict, selected: Path) -> Path:
    """Проверяет и сохраняет выбранную папку после явного запроса настройки."""

    resolved = selected.expanduser().resolve()
    workspace_root = root.resolve()
    if (
        resolved == workspace_root
        or resolved in workspace_root.parents
        or workspace_root in resolved.parents
    ):
        raise ValueError(
            "папка хранилища не должна содержать Workspace или находиться внутри него"
        )
    ok, message = storage_status(resolved)
    if not ok:
        raise ValueError(message)
    verify_storage_write(resolved)

    write_json_atomic(
        external_storage_config_path(root),
        {
            "schemaVersion": 1,
            "root": str(resolved),
            "configuredAt": datetime.now(timezone.utc).isoformat(),
        },
        private=True,
    )
    enable_integration(workspace, "external-file-storage")
    write_json_atomic(root / "workspace.json", workspace)
    return resolved


def choose_storage_path(argument: str | None) -> Path:
    """Получает путь из аргумента либо из терминального запроса пользователя."""

    if argument:
        return Path(argument)
    if not sys.stdin.isatty():
        raise ValueError("для неинтерактивного --setup укажите --path ПАПКА")
    entered = input("Локальная папка внешнего хранилища: ").strip()
    if not entered:
        raise ValueError("папка не выбрана")
    return Path(entered)


def list_integrations() -> None:
    for name, description in INTEGRATIONS.items():
        print(f"{name}: {description}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("integration", nargs="?", choices=sorted(INTEGRATIONS))
    parser.add_argument("--list", action="store_true", dest="show_list")
    parser.add_argument(
        "--setup",
        action="store_true",
        help="явно настроить external-file-storage; обычный preflight ничего не меняет",
    )
    parser.add_argument(
        "--path",
        help="локальная папка для external-file-storage; используется только с --setup",
    )
    args = parser.parse_args()

    if args.show_list:
        list_integrations()
        return 0
    if not args.integration:
        parser.error("укажите интеграцию или --list")
    if args.setup and args.integration != "external-file-storage":
        parser.error("--setup поддерживается только для external-file-storage")
    if args.path and not args.setup:
        parser.error("--path требует --setup")

    try:
        workspace = load_workspace(ROOT)
    except ValueError as exc:
        print(exc)
        return 2

    if not workspace or workspace.get("initialized") is not True:
        print("Сначала завершите первичную настройку через $setup-workspace.")
        return 2

    system = platform.system() or "Unknown"
    storage_ready = True
    print(f"Интеграция: {args.integration}")
    print(f"Платформа: {system}")
    print(f"Режим: {INTEGRATIONS[args.integration]}")

    if args.integration == "external-file-storage":
        if args.setup:
            try:
                selected = setup_external_storage(
                    ROOT,
                    workspace,
                    choose_storage_path(args.path),
                )
            except (OSError, ValueError) as exc:
                print(f"Настройка не выполнена: {exc}")
                return 4
            print(f"Локальная папка сохранена: {selected}")
            print("Проверка записи, чтения и удаления пройдена.")
        else:
            storage_ready, message = storage_status(load_external_storage(ROOT))
            print(f"Локальное хранилище: {message}.")
            if not storage_ready:
                print(
                    "Настройка: python3 scripts/integration_preflight.py "
                    "external-file-storage --setup --path ПАПКА"
                )

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

    if args.integration == "external-file-storage" and not storage_ready:
        print("Статус: включена, но локальная папка не настроена на этом компьютере.")
        return 4

    print("Статус: конфигурация включена; продолжите через соответствующий навык.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
