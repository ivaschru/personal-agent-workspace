#!/usr/bin/env python3
"""Создаёт и проверяет зашифрованные recovery-архивы локальных секретов.

Скрипт намеренно делегирует криптографию `age`: Workspace отвечает за
allowlist, упаковку, атомарную запись и проверку содержимого, но не реализует
собственный шифр или формат ключей.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import Iterable


ROOT = Path(__file__).resolve().parents[3]
PLAN_PATH = ROOT / "recovery-plan.json"
PLAN_TEMPLATE = ROOT / "skills/encrypted-recovery/assets/recovery-plan.example.json"
EXTERNAL_STORAGE_CONFIG = ROOT / ".local/external-file-storage.json"
WORK_DIRECTORY = ROOT / ".local/recovery-work"
DEFAULT_RESTORE_ROOT = ROOT / ".local/recovery-restore"
ARCHIVE_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")
TIMESTAMP_PATTERN = re.compile(r"-(\d{8}T\d{6}Z)\.tar\.age\Z")
MANIFEST_NAME = ".recovery-manifest.json"
FORMAT_VERSION = 1


class RecoveryError(RuntimeError):
    """Ошибка конфигурации или безопасного выполнения recovery."""


@dataclass(frozen=True)
class ArchivePlan:
    """Проверенный scope одного независимого recovery-архива."""

    archive_id: str
    description: str
    sources: tuple[str, ...]
    required: bool


@dataclass(frozen=True)
class RetentionPolicy:
    """Календарные границы хранения датированных копий."""

    keep_all_days: int
    keep_weekly_days: int
    keep_monthly_days: int


@dataclass(frozen=True)
class RecoveryPlan:
    """Полностью проверенная переносимая конфигурация recovery."""

    storage_subdir: str
    retention: RetentionPolicy
    archives: tuple[ArchivePlan, ...]


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RecoveryError(f"Файл не найден: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryError(f"Не удалось прочитать JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RecoveryError(f"Корень JSON должен быть объектом: {path}")
    return value


def write_json_atomic(path: Path, value: dict, *, private: bool = False) -> None:
    """Атомарно заменяет JSON, не оставляя частично записанный файл."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if private and os.name == "posix":
        temporary.chmod(0o600)
    os.replace(temporary, path)


def validate_private_workspace() -> dict:
    """Не позволяет настраивать recovery в публичной или сырой копии."""

    workspace = read_json(ROOT / "workspace.json")
    if workspace.get("initialized") is not True:
        raise RecoveryError("Сначала завершите первичную настройку Workspace.")
    visibility = workspace.get("privacy", {}).get("repositoryVisibility")
    if visibility not in {"private", "local-only"}:
        raise RecoveryError(
            "Recovery разрешён только после подтверждения private или local-only видимости."
        )
    return workspace


def safe_relative_path(value: object, *, label: str) -> str:
    """Проверяет POSIX-путь до его объединения с доверенным корнем."""

    if not isinstance(value, str) or not value.strip():
        raise RecoveryError(f"{label}: ожидается непустой относительный путь")
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or ".." in candidate.parts or "." in candidate.parts:
        raise RecoveryError(f"{label}: путь должен быть относительным и без . или ..")
    normalized = candidate.as_posix()
    if normalized.startswith("/"):
        raise RecoveryError(f"{label}: абсолютный путь запрещён")
    return normalized


def positive_int(value: object, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RecoveryError(f"{label}: ожидается неотрицательное целое число")
    return value


def load_plan() -> RecoveryPlan:
    raw = read_json(PLAN_PATH)
    if raw.get("schemaVersion") != 1:
        raise RecoveryError("recovery-plan.json: поддерживается только schemaVersion=1")
    if raw.get("encryption") != "age-passphrase":
        raise RecoveryError("recovery-plan.json: encryption должен быть age-passphrase")

    storage_subdir = safe_relative_path(
        raw.get("storageSubdir"), label="storageSubdir"
    )
    retention_raw = raw.get("retention")
    if not isinstance(retention_raw, dict):
        raise RecoveryError("recovery-plan.json: отсутствует объект retention")
    retention = RetentionPolicy(
        keep_all_days=positive_int(
            retention_raw.get("keepAllDays"), label="retention.keepAllDays"
        ),
        keep_weekly_days=positive_int(
            retention_raw.get("keepWeeklyDays"), label="retention.keepWeeklyDays"
        ),
        keep_monthly_days=positive_int(
            retention_raw.get("keepMonthlyDays"), label="retention.keepMonthlyDays"
        ),
    )
    if not (
        retention.keep_all_days
        <= retention.keep_weekly_days
        <= retention.keep_monthly_days
    ):
        raise RecoveryError("Границы retention должны возрастать: all <= weekly <= monthly")

    archives_raw = raw.get("archives")
    if not isinstance(archives_raw, list) or not archives_raw:
        raise RecoveryError("recovery-plan.json: archives должен быть непустым массивом")

    seen_ids: set[str] = set()
    archives: list[ArchivePlan] = []
    for index, item in enumerate(archives_raw):
        if not isinstance(item, dict):
            raise RecoveryError(f"archives[{index}]: ожидается объект")
        archive_id = item.get("id")
        if not isinstance(archive_id, str) or not ARCHIVE_ID_PATTERN.fullmatch(archive_id):
            raise RecoveryError(
                f"archives[{index}].id: используйте 1-63 символа a-z, 0-9 и дефис"
            )
        if archive_id in seen_ids:
            raise RecoveryError(f"Повторяющийся archive id: {archive_id}")
        seen_ids.add(archive_id)

        sources_raw = item.get("sources")
        if not isinstance(sources_raw, list):
            raise RecoveryError(f"archives[{index}].sources: ожидается массив")
        sources: list[str] = []
        for source_index, source in enumerate(sources_raw):
            normalized = safe_relative_path(
                source,
                label=f"archives[{index}].sources[{source_index}]",
            )
            if not normalized.startswith(".local/"):
                raise RecoveryError(
                    f"{normalized}: укажите точный путь внутри .local/, а не весь каталог"
                )
            sources.append(normalized)

        description = item.get("description", "")
        if not isinstance(description, str):
            raise RecoveryError(f"archives[{index}].description: ожидается строка")
        required = item.get("required", True)
        if not isinstance(required, bool):
            raise RecoveryError(f"archives[{index}].required: ожидается true или false")
        archives.append(ArchivePlan(archive_id, description, tuple(sources), required))

    return RecoveryPlan(storage_subdir, retention, tuple(archives))


def load_external_storage() -> Path:
    data = read_json(EXTERNAL_STORAGE_CONFIG)
    selected = data.get("root")
    if not isinstance(selected, str) or not selected:
        raise RecoveryError("В конфигурации external-file-storage отсутствует root.")
    root = Path(selected).expanduser().resolve()
    if not root.is_dir():
        raise RecoveryError(f"Папка внешнего хранилища недоступна: {root}")
    if not os.access(root, os.R_OK | os.W_OK | os.X_OK):
        raise RecoveryError(f"Недостаточно прав для внешнего хранилища: {root}")
    return root


def storage_directory(plan: RecoveryPlan) -> Path:
    root = load_external_storage()
    destination = (root / plan.storage_subdir).resolve()
    if destination != root and root not in destination.parents:
        raise RecoveryError("storageSubdir выходит за пределы внешнего хранилища")
    return destination


def age_executable() -> str:
    executable = shutil.which("age") or shutil.which("rage")
    if not executable:
        raise RecoveryError(
            "Не найден age. Установите официальный age/rage и повторите проверку."
        )
    return executable


def run_age(*, decrypt: bool, source: Path, destination: Path) -> None:
    """Запускает интерактивный парольный режим официального `age`.

    `age` намеренно читает passphrase напрямую из терминала. Не подменяем это
    `expect`, environment-переменной или временным password-файлом: такие
    обходы ухудшили бы модель утечки, ради которой выбран готовый backend.
    """

    command = [age_executable()]
    if decrypt:
        command.append("--decrypt")
    else:
        command.append("--passphrase")
    command.extend(["--output", str(destination), str(source)])
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise RecoveryError(f"Не удалось запустить age: {exc}") from exc
    if completed.returncode != 0:
        # Сообщение age не содержит пароль, но ограничиваем вывод одной строкой,
        # чтобы случайный debug-режим не попал в журнал агента целиком.
        detail = completed.stderr.strip().splitlines()[-1:] or ["неизвестная ошибка"]
        raise RecoveryError(f"age завершился с ошибкой: {detail[0]}")


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_source_files(source: Path) -> Iterable[Path]:
    """Обходит каталог без следования по symlink и special-файлам."""

    if source.is_symlink():
        raise RecoveryError(f"Symlink запрещён в recovery allowlist: {source}")
    if source.is_file():
        yield source
        return
    if not source.is_dir():
        raise RecoveryError(f"Источник не является обычным файлом или каталогом: {source}")

    for directory, directory_names, file_names in os.walk(source, followlinks=False):
        current = Path(directory)
        directory_names.sort()
        for name in list(directory_names):
            child = current / name
            if child.is_symlink():
                raise RecoveryError(f"Symlink запрещён внутри recovery-каталога: {child}")
        for name in sorted(file_names):
            child = current / name
            mode = child.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise RecoveryError(f"Symlink запрещён внутри recovery-каталога: {child}")
            if not stat.S_ISREG(mode):
                raise RecoveryError(f"Special-файл запрещён в recovery: {child}")
            yield child


def validate_source_location(source_value: str) -> Path:
    """Не даёт промежуточному symlink вывести чтение за пределы `.local/`.

    Проверки одного `source.is_symlink()` недостаточно: путь вида
    `.local/link/secret` может указывать наружу, хотя конечный файл сам не
    является symlink. Поэтому проверяем каждый уже существующий компонент и
    дополнительно сравниваем физический resolved-путь с корнем `.local`.
    """

    source = ROOT / source_value
    local_root = ROOT / ".local"
    current = ROOT
    for component in PurePosixPath(source_value).parts:
        current = current / component
        if current.exists() or current.is_symlink():
            if current.is_symlink():
                raise RecoveryError(f"Symlink запрещён в recovery-пути: {current}")
    if source.exists():
        resolved_local = local_root.resolve()
        resolved_source = source.resolve()
        if resolved_source == resolved_local or resolved_local not in resolved_source.parents:
            raise RecoveryError(f"Recovery-путь выходит за пределы .local/: {source_value}")
    return source


def collect_files(plan: ArchivePlan) -> tuple[list[Path], list[str]]:
    if not plan.sources:
        raise RecoveryError(f"Archive {plan.archive_id}: allowlist sources пока пуст.")
    files: dict[str, Path] = {}
    missing: list[str] = []
    for source_value in plan.sources:
        source = validate_source_location(source_value)
        if not source.exists() and not source.is_symlink():
            missing.append(source_value)
            continue
        for path in iter_source_files(source):
            relative = path.relative_to(ROOT).as_posix()
            files[relative] = path
    if missing and plan.required:
        raise RecoveryError(
            f"Archive {plan.archive_id}: отсутствуют обязательные пути: {', '.join(missing)}"
        )
    if not files:
        raise RecoveryError(f"Archive {plan.archive_id}: нет файлов для backup.")
    return [files[key] for key in sorted(files)], missing


def build_plain_archive(plan: ArchivePlan, destination: Path) -> dict:
    files, missing = collect_files(plan)
    entries: list[dict] = []
    for path in files:
        relative = path.relative_to(ROOT).as_posix()
        entries.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": hash_file(path),
            }
        )
    manifest = {
        "formatVersion": FORMAT_VERSION,
        "archiveId": plan.archive_id,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "missingOptionalSources": missing,
        "files": entries,
    }
    manifest_bytes = json.dumps(
        manifest, ensure_ascii=False, indent=2
    ).encode("utf-8") + b"\n"

    with tarfile.open(destination, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        info = tarfile.TarInfo(MANIFEST_NAME)
        info.size = len(manifest_bytes)
        info.mode = 0o600
        info.mtime = int(datetime.now(timezone.utc).timestamp())
        archive.addfile(info, io.BytesIO(manifest_bytes))
        for path in files:
            relative = path.relative_to(ROOT).as_posix()
            archive.add(path, arcname=f"files/{relative}", recursive=False)
    if os.name == "posix":
        destination.chmod(0o600)
    return manifest


def validate_member_name(name: str) -> str:
    candidate = PurePosixPath(name)
    if candidate.is_absolute() or ".." in candidate.parts or "." in candidate.parts:
        raise RecoveryError(f"Небезопасный путь внутри recovery-архива: {name}")
    return candidate.as_posix()


def verify_plain_archive(path: Path, *, expected_id: str | None = None) -> dict:
    """Проверяет закрытый manifest, состав tar и hash каждого файла."""

    try:
        archive = tarfile.open(path, "r:gz")
    except (OSError, tarfile.TarError) as exc:
        raise RecoveryError(f"Некорректный recovery tar: {exc}") from exc
    with archive:
        members = archive.getmembers()
        names = [validate_member_name(member.name) for member in members]
        if len(names) != len(set(names)):
            raise RecoveryError("Recovery tar содержит повторяющиеся пути.")
        try:
            manifest_member = archive.getmember(MANIFEST_NAME)
        except KeyError as exc:
            raise RecoveryError("В recovery tar отсутствует manifest.") from exc
        if not manifest_member.isfile() or manifest_member.size > 10 * 1024 * 1024:
            raise RecoveryError("Manifest recovery tar имеет недопустимый тип или размер.")
        handle = archive.extractfile(manifest_member)
        if handle is None:
            raise RecoveryError("Не удалось прочитать manifest recovery tar.")
        try:
            manifest = json.loads(handle.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecoveryError(f"Некорректный manifest recovery tar: {exc}") from exc

        if manifest.get("formatVersion") != FORMAT_VERSION:
            raise RecoveryError("Неподдерживаемая версия recovery archive format.")
        archive_id = manifest.get("archiveId")
        if expected_id is not None and archive_id != expected_id:
            raise RecoveryError(
                f"Archive ID не совпадает: ожидался {expected_id}, получен {archive_id}"
            )
        entries = manifest.get("files")
        if not isinstance(entries, list) or not entries:
            raise RecoveryError("Manifest не содержит файлов.")

        expected_members = {MANIFEST_NAME}
        manifest_paths: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise RecoveryError("Некорректная запись файла в manifest.")
            path_value = entry.get("path")
            expected_size = entry.get("size")
            expected_hash = entry.get("sha256")
            if not isinstance(path_value, str):
                raise RecoveryError("Manifest path должен быть строкой.")
            if (
                not isinstance(expected_size, int)
                or isinstance(expected_size, bool)
                or expected_size < 0
            ):
                raise RecoveryError(f"Некорректный размер в manifest: {path_value}")
            if not isinstance(expected_hash, str) or not re.fullmatch(
                r"[0-9a-f]{64}", expected_hash
            ):
                raise RecoveryError(f"Некорректный SHA-256 в manifest: {path_value}")
            relative = validate_member_name(path_value)
            if not relative.startswith(".local/"):
                raise RecoveryError(f"Manifest содержит путь вне .local/: {relative}")
            if relative in manifest_paths:
                raise RecoveryError(f"Manifest повторяет путь: {relative}")
            manifest_paths.add(relative)
            member_name = f"files/{relative}"
            expected_members.add(member_name)
            try:
                member = archive.getmember(member_name)
            except KeyError as exc:
                raise RecoveryError(f"В tar отсутствует файл из manifest: {relative}") from exc
            if not member.isfile():
                raise RecoveryError(f"В recovery ожидается обычный файл: {relative}")
            extracted = archive.extractfile(member)
            if extracted is None:
                raise RecoveryError(f"Не удалось прочитать файл recovery: {relative}")
            digest = hashlib.sha256()
            size = 0
            for chunk in iter(lambda: extracted.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
            if size != expected_size or digest.hexdigest() != expected_hash:
                raise RecoveryError(f"Проверка SHA-256 не пройдена: {relative}")

        unexpected = set(names) - expected_members
        if unexpected:
            raise RecoveryError(
                "Recovery tar содержит файлы вне manifest: " + ", ".join(sorted(unexpected))
            )
        return manifest


def decrypt_and_verify(
    encrypted: Path, plain: Path, *, archive_id: str
) -> dict:
    run_age(decrypt=True, source=encrypted, destination=plain)
    if os.name == "posix":
        plain.chmod(0o600)
    return verify_plain_archive(plain, expected_id=archive_id)


def archive_path(directory: Path, archive_id: str, *, latest: bool) -> Path:
    suffix = "latest" if latest else datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return directory / f"{archive_id}-{suffix}.tar.age"


def backup_one(
    plan: ArchivePlan,
    directory: Path,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    WORK_DIRECTORY.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        WORK_DIRECTORY.chmod(0o700)

    final_path = archive_path(directory, plan.archive_id, latest=False)
    if final_path.exists():
        raise RecoveryError(f"Датированный архив уже существует: {final_path}")

    with tempfile.TemporaryDirectory(prefix="backup-", dir=WORK_DIRECTORY) as temporary_name:
        temporary = Path(temporary_name)
        plain = temporary / "archive.tar.gz"
        encrypted = directory / f".{final_path.name}.tmp"
        verification_plain = temporary / "verification.tar.gz"
        build_plain_archive(plan, plain)
        try:
            run_age(
                decrypt=False,
                source=plain,
                destination=encrypted,
            )
            if os.name == "posix":
                encrypted.chmod(0o600)
            decrypt_and_verify(
                encrypted,
                verification_plain,
                archive_id=plan.archive_id,
            )
            os.replace(encrypted, final_path)
        finally:
            encrypted.unlink(missing_ok=True)

    latest_path = archive_path(directory, plan.archive_id, latest=True)
    latest_temporary = directory / f".{latest_path.name}.tmp"
    try:
        shutil.copyfile(final_path, latest_temporary)
        if os.name == "posix":
            latest_temporary.chmod(0o600)
        os.replace(latest_temporary, latest_path)
    finally:
        latest_temporary.unlink(missing_ok=True)
    return final_path


def parse_archive_time(path: Path) -> datetime | None:
    match = TIMESTAMP_PATTERN.search(path.name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def retention_keep_set(
    paths: list[Path], policy: RetentionPolicy, *, now: datetime
) -> set[Path]:
    """Выбирает последние weekly/monthly точки, сохраняя свежие копии целиком."""

    keep: set[Path] = set()
    weekly: dict[tuple[int, int], tuple[datetime, Path]] = {}
    monthly: dict[tuple[int, int], tuple[datetime, Path]] = {}
    for path in paths:
        created = parse_archive_time(path)
        if created is None:
            # Не удаляем файл неизвестного формата: retention не должен
            # превращаться в общий cleanup внешнего каталога.
            keep.add(path)
            continue
        age = now - created
        if age <= timedelta(days=policy.keep_all_days):
            keep.add(path)
        elif age <= timedelta(days=policy.keep_weekly_days):
            iso = created.isocalendar()
            key = (iso.year, iso.week)
            if key not in weekly or created > weekly[key][0]:
                weekly[key] = (created, path)
        elif age <= timedelta(days=policy.keep_monthly_days):
            key = (created.year, created.month)
            if key not in monthly or created > monthly[key][0]:
                monthly[key] = (created, path)
    keep.update(value[1] for value in weekly.values())
    keep.update(value[1] for value in monthly.values())
    return keep


def retention_removals(
    directory: Path,
    plan: ArchivePlan,
    policy: RetentionPolicy,
    *,
    now: datetime | None = None,
) -> list[Path]:
    """Возвращает кандидатов без удаления файлов внешнего хранилища."""

    candidates = sorted(directory.glob(f"{plan.archive_id}-*.tar.age"))
    candidates = [path for path in candidates if "-latest.tar.age" not in path.name]
    keep = retention_keep_set(
        candidates,
        policy,
        now=now or datetime.now(timezone.utc),
    )
    return [path for path in candidates if path not in keep]


def select_archives(plan: RecoveryPlan, archive_id: str | None) -> list[ArchivePlan]:
    if archive_id is None:
        return list(plan.archives)
    selected = [item for item in plan.archives if item.archive_id == archive_id]
    if not selected:
        raise RecoveryError(f"Archive id отсутствует в recovery-plan.json: {archive_id}")
    return selected


def command_init(args: argparse.Namespace) -> None:
    workspace = validate_private_workspace()
    load_external_storage()
    age_executable()
    if PLAN_PATH.exists() and not args.force:
        raise RecoveryError("recovery-plan.json уже существует; используйте --force осознанно.")
    # План является tracked пользовательской политикой, поэтому создаём его
    # атомарно и никогда не оставляем усечённый JSON после сбоя записи.
    write_json_atomic(PLAN_PATH, read_json(PLAN_TEMPLATE))

    storage = workspace.setdefault("storage", {})
    storage["backup"] = "encrypted-recovery"
    storage["recoveryPlan"] = "recovery-plan.json"
    write_json_atomic(ROOT / "workspace.json", workspace)
    print("Создан recovery-plan.json с пустым allowlist.")
    print("Добавьте только подтверждённые пути внутри .local/ и выполните status.")


def command_status(args: argparse.Namespace) -> None:
    validate_private_workspace()
    plan = load_plan()
    directory = storage_directory(plan)
    executable = age_executable()
    print(f"Шифрование: {Path(executable).name}")
    print(f"Внешний каталог: {directory}")
    for archive in select_archives(plan, args.archive):
        present = 0
        missing = 0
        for source in archive.sources:
            if (ROOT / source).exists():
                present += 1
            else:
                missing += 1
        latest = archive_path(directory, archive.archive_id, latest=True)
        print(
            f"{archive.archive_id}: sources={len(archive.sources)}, "
            f"present={present}, missing={missing}, latest={'yes' if latest.exists() else 'no'}"
        )


def command_backup(args: argparse.Namespace) -> None:
    validate_private_workspace()
    plan = load_plan()
    directory = storage_directory(plan)
    selected = select_archives(plan, args.archive)
    for archive in selected:
        created = backup_one(archive, directory)
        print(f"Создан и проверен: {created}")
    print("Старые копии не удалялись: сначала подтвердите внешнюю синхронизацию.")


def command_prune(args: argparse.Namespace) -> None:
    validate_private_workspace()
    plan = load_plan()
    directory = storage_directory(plan)
    removals: list[Path] = []
    for archive in select_archives(plan, args.archive):
        removals.extend(retention_removals(directory, archive, plan.retention))
    if not removals:
        print("Retention: удаление не требуется.")
        return
    print("Retention candidates:")
    for path in removals:
        print(f"- {path.name}")
    if not args.confirm_synced:
        print("Dry run: файлы не удалены. После проверки синхронизации добавьте --confirm-synced.")
        return
    for path in removals:
        path.unlink()
    print(f"Удалено подтверждённых старых копий: {len(removals)}")


def command_verify(args: argparse.Namespace) -> None:
    validate_private_workspace()
    plan = load_plan()
    directory = storage_directory(plan)
    selected = select_archives(plan, args.archive)
    WORK_DIRECTORY.mkdir(parents=True, exist_ok=True)
    for archive in selected:
        encrypted = archive_path(directory, archive.archive_id, latest=True)
        if not encrypted.is_file():
            raise RecoveryError(f"Latest archive не найден: {encrypted}")
        with tempfile.TemporaryDirectory(prefix="verify-", dir=WORK_DIRECTORY) as name:
            manifest = decrypt_and_verify(
                encrypted,
                Path(name) / "archive.tar.gz",
                archive_id=archive.archive_id,
            )
        print(f"Проверен {archive.archive_id}: файлов {len(manifest['files'])}.")


def safe_output_path(root: Path, relative: str) -> Path:
    destination = (root / PurePosixPath(relative)).resolve()
    resolved_root = root.resolve()
    if destination == resolved_root or resolved_root not in destination.parents:
        raise RecoveryError(f"Путь восстановления выходит за пределы output: {relative}")
    return destination


def extract_verified_archive(plain: Path, manifest: dict, output: Path) -> None:
    if output.exists():
        raise RecoveryError(f"Папка восстановления уже существует: {output}")
    output.mkdir(parents=True, mode=0o700)
    try:
        with tarfile.open(plain, "r:gz") as archive:
            for entry in manifest["files"]:
                relative = entry["path"]
                destination = safe_output_path(output, relative)
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                member = archive.getmember(f"files/{relative}")
                source = archive.extractfile(member)
                if source is None:
                    raise RecoveryError(f"Не удалось извлечь: {relative}")
                with destination.open("xb") as target:
                    shutil.copyfileobj(source, target)
                if os.name == "posix":
                    destination.chmod(0o600)
    except Exception:
        shutil.rmtree(output, ignore_errors=True)
        raise


def command_restore(args: argparse.Namespace) -> None:
    validate_private_workspace()
    plan = load_plan()
    directory = storage_directory(plan)
    archive = select_archives(plan, args.archive)[0]
    encrypted = archive_path(directory, archive.archive_id, latest=True)
    if not encrypted.is_file():
        raise RecoveryError(f"Latest archive не найден: {encrypted}")
    WORK_DIRECTORY.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="restore-", dir=WORK_DIRECTORY) as name:
        plain = Path(name) / "archive.tar.gz"
        manifest = decrypt_and_verify(
            encrypted,
            plain,
            archive_id=archive.archive_id,
        )
        if args.output:
            output = Path(args.output).expanduser().resolve()
        else:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            output = DEFAULT_RESTORE_ROOT / f"{archive.archive_id}-{timestamp}"
        extract_verified_archive(plain, manifest, output)
    print(f"Восстановлено после проверки: {output}")
    print("Исходные рабочие пути не изменены; просмотрите файлы перед переносом.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="создать безопасный пустой план")
    init_parser.add_argument("--force", action="store_true")
    init_parser.set_defaults(handler=command_init)

    for name, handler in (
        ("status", command_status),
        ("backup", command_backup),
        ("verify", command_verify),
    ):
        command_parser = subparsers.add_parser(name)
        command_parser.add_argument("--archive", help="обработать только указанный scope")
        command_parser.set_defaults(handler=handler)

    prune_parser = subparsers.add_parser(
        "prune", help="показать или удалить старые копии по retention"
    )
    prune_parser.add_argument("--archive", help="обработать только указанный scope")
    prune_parser.add_argument(
        "--confirm-synced",
        action="store_true",
        help="удалить кандидатов после внешнего подтверждения синхронизации",
    )
    prune_parser.set_defaults(handler=command_prune)

    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("--archive", required=True, help="archive id из плана")
    restore_parser.add_argument(
        "--output", help="новая папка для проверенного plaintext-восстановления"
    )
    restore_parser.set_defaults(handler=command_restore)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.handler(args)
    except RecoveryError as exc:
        print(f"Recovery не выполнен: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
