#!/usr/bin/env python3
"""Проверяет и применяет release-based обновления Personal Codex Workspace.

Скрипт меняет только управляемые manifest-файлы и служебные поля обновления в
workspace.json. Режим ``--auto`` сам создаёт отдельный Git worktree, проверяет
результат, фиксирует его коммитом и безопасно переносит в рабочую ветку.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path


# Внешний root нужен только для однократного bootstrap старых копий, в которых
# updater ещё отсутствует. Обычный запуск по-прежнему автоматически использует
# репозиторий, содержащий этот файл.
ROOT = Path(
    os.environ.get("PERSONAL_CODEX_WORKSPACE_ROOT", Path(__file__).resolve().parent.parent)
).resolve()
UPDATER_VERSION = "1.0.0"
STATE_PATH = ROOT / ".local/template-update-state.json"
WORKSPACE_PATH = ROOT / "workspace.json"
MANIFEST_PATH = ROOT / "template-manifest.json"
EXIT_CONFLICT = 3
EXIT_VALIDATION = 4
EXIT_UPDATE_AVAILABLE = 10
EXIT_USER_ACTION = 20


def parse_version(value: str) -> tuple[int, int, int]:
    """Разбирает строгий stable SemVer без prerelease-суффиксов."""

    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", value.strip())
    if not match:
        raise ValueError(f"Неподдерживаемая версия: {value}")
    return tuple(int(part) for part in match.groups())


def plain_version(value: str) -> str:
    parsed = parse_version(value)
    return ".".join(str(part) for part in parsed)


def matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_private(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        # Windows может управлять доступом через ACL и не поддерживать POSIX mode.
        pass


def repository_coordinates(source: str) -> tuple[str, str]:
    parsed = urllib.parse.urlparse(source)
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com":
        raise ValueError("template.source должен быть HTTPS URL на github.com")
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) != 2:
        raise ValueError("template.source должен иметь вид https://github.com/OWNER/REPO")
    return parts[0], parts[1].removesuffix(".git")


def request_json(url: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "personal-codex-workspace-updater",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def latest_release(source: str) -> dict:
    owner, repo = repository_coordinates(source)
    return request_json(f"https://api.github.com/repos/{owner}/{repo}/releases/latest")


def read_workspace() -> dict:
    if not WORKSPACE_PATH.exists():
        raise ValueError("workspace.json отсутствует; сначала выполните setup-workspace")
    workspace = load_json(WORKSPACE_PATH)
    if workspace.get("initialized") is not True:
        raise ValueError("workspace.json ещё не инициализирован")
    return workspace


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def is_check_due(workspace: dict, state: dict) -> bool:
    interval = int(workspace.get("updates", {}).get("checkIntervalHours", 24))
    raw = state.get("lastCheckedAt")
    if not raw:
        return True
    try:
        previous = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    return utc_now() - previous >= dt.timedelta(hours=max(interval, 1))


def check_update(if_due: bool) -> int:
    workspace = read_workspace()
    updates = workspace.get("updates", {})
    if updates.get("autoCheck", True) is not True:
        print("Автоматическая проверка обновлений выключена.")
        return 0

    state = load_json(STATE_PATH) if STATE_PATH.exists() else {}
    if if_due and not is_check_due(workspace, state):
        print("Проверка обновлений ещё не требуется.")
        return 0

    release = latest_release(workspace["template"]["source"])
    latest = plain_version(release["tag_name"])
    current = plain_version(workspace["template"]["version"])
    state.update(
        {
            "lastCheckedAt": utc_now().isoformat().replace("+00:00", "Z"),
            "latestVersion": latest,
            "releaseUrl": release.get("html_url"),
            "pending": parse_version(latest) > parse_version(current),
        }
    )
    write_json_private(STATE_PATH, state)

    if parse_version(latest) > parse_version(current):
        print(f"Доступно обновление {current} -> {latest}")
        return EXIT_UPDATE_AVAILABLE
    if state.get("pushPending") is True:
        print("Обновление установлено локально; требуется повторить автоматический push.")
        return EXIT_UPDATE_AVAILABLE
    print(f"Текущая версия {current} актуальна.")
    return 0


def safe_extract(archive: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:*") as bundle:
        for member in bundle.getmembers():
            member_path = Path(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise ValueError("Release archive содержит небезопасный путь")
            if member.issym() or member.islnk():
                raise ValueError("Release archive содержит ссылку; обновление остановлено")
        bundle.extractall(destination)

    roots = [path for path in destination.iterdir() if path.is_dir()]
    if len(roots) != 1:
        raise ValueError("Не удалось определить корень release archive")
    return roots[0]


def download_tag(source: str, tag: str, destination: Path) -> Path:
    owner, repo = repository_coordinates(source)
    # TemporaryDirectory создаёт только общий root. Для base/target используются
    # отдельные подпапки, поэтому их нужно создать до открытия archive на запись.
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / f"{tag}.tar.gz"
    request = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}/tarball/{tag}",
        headers={"User-Agent": "personal-codex-workspace-updater"},
    )
    with urllib.request.urlopen(request, timeout=60) as response, archive.open("wb") as output:
        shutil.copyfileobj(response, output)
    return safe_extract(archive, destination / tag)


def collect_managed(root: Path, manifest: dict) -> set[str]:
    managed = list(manifest.get("managedPatterns", []))
    protected = list(manifest.get("protectedPatterns", []))
    result: set[str] = set()
    for path in root.rglob("*"):
        if (
            not path.is_file()
            or ".git" in path.parts
            or "__pycache__" in path.parts
            or path.suffix in {".pyc", ".pyo"}
        ):
            continue
        relative = path.relative_to(root).as_posix()
        if matches(relative, managed) and not matches(relative, protected):
            result.add(relative)
    return result


def verify_release(root: Path, manifest: dict, target_version: str) -> None:
    if plain_version(manifest.get("version", "")) != plain_version(target_version):
        raise ValueError("Версия manifest не совпадает с release tag")
    expected_files = collect_managed(root, manifest) - {"template-manifest.json"}
    declared = set(manifest.get("files", {}))
    if expected_files != declared:
        raise ValueError("Список файлов release не совпадает с manifest")
    for relative, expected in manifest.get("files", {}).items():
        actual = hashlib.sha256((root / relative).read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"SHA-256 не совпадает: {relative}")


def read_optional(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() and path.is_file() else None


def decide_file_action(base: bytes | None, current: bytes | None, target: bytes | None) -> str:
    """Классифицирует трёхстороннее изменение без привязки к файловой системе."""

    if base == target or current == target:
        return "noop"
    if current == base:
        if target is None:
            return "delete"
        return "add" if base is None else "replace"
    if target is None and current is None:
        return "noop"
    return "merge"


def text_bytes(value: bytes | None) -> bool:
    if value is None or b"\x00" in value:
        return False
    try:
        value.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def merge_text(current: bytes, base: bytes, target: bytes) -> tuple[bytes, bool]:
    with tempfile.TemporaryDirectory(prefix="template-merge-") as temp:
        directory = Path(temp)
        ours = directory / "current"
        ancestor = directory / "base"
        theirs = directory / "incoming"
        ours.write_bytes(current)
        ancestor.write_bytes(base)
        theirs.write_bytes(target)
        result = subprocess.run(
            [
                "git",
                "merge-file",
                "-p",
                "--diff3",
                "-L",
                "current",
                "-L",
                "base",
                "-L",
                "incoming",
                str(ours),
                str(ancestor),
                str(theirs),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    if result.returncode not in {0, 1}:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
    return result.stdout, result.returncode == 0


def apply_files(base_root: Path, target_root: Path, manifest: dict) -> list[str]:
    protected = list(manifest.get("protectedPatterns", []))
    paths = collect_managed(base_root, manifest) | collect_managed(target_root, manifest)
    conflicts: list[str] = []

    for relative in sorted(paths):
        if matches(relative, protected):
            continue
        base = read_optional(base_root / relative)
        current = read_optional(ROOT / relative)
        target = read_optional(target_root / relative)
        action = decide_file_action(base, current, target)
        destination = ROOT / relative

        if action == "noop":
            continue
        if action == "delete":
            destination.unlink(missing_ok=True)
            continue
        if action in {"add", "replace"}:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target_root / relative, destination)
            continue

        if all(text_bytes(value) for value in (base, current, target)):
            merged, clean = merge_text(current or b"", base or b"", target or b"")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(merged)
            if clean:
                continue
        conflicts.append(relative)

    return conflicts


def expand_command(command: list[str]) -> list[str]:
    return [
        sys.executable if item == "{python}" else str(ROOT) if item == "{root}" else item
        for item in command
    ]


def run_commands(commands: list[list[str]], label: str) -> None:
    for command in commands:
        expanded = expand_command(command)
        print(f"{label}: {' '.join(expanded)}")
        result = subprocess.run(expanded, cwd=ROOT, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"Команда завершилась с кодом {result.returncode}")


def update_workspace_version(version: str) -> None:
    workspace = read_workspace()
    workspace.setdefault("template", {})["version"] = plain_version(version)
    workspace.setdefault("updates", {})["lastAppliedAt"] = utc_now().isoformat().replace(
        "+00:00", "Z"
    )
    WORKSPACE_PATH.write_text(
        json.dumps(workspace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def ensure_clean_worktree() -> None:
    result = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ROOT, text=True, capture_output=True, check=True
    )
    if result.stdout.strip():
        raise ValueError("Update worktree должен быть чистым перед применением")


def git(*args: str, cwd: Path = ROOT, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Запускает Git без shell, чтобы пути и ветки не интерпретировались оболочкой."""

    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=check
    )


def changed_paths(root: Path) -> set[str]:
    tracked = git("diff", "--name-only", "HEAD", cwd=root).stdout.splitlines()
    untracked = git("ls-files", "--others", "--exclude-standard", cwd=root).stdout.splitlines()
    return {path.strip() for path in tracked + untracked if path.strip()}


def validate_changed_paths(root: Path, manifest: dict) -> None:
    managed = list(manifest.get("managedPatterns", []))
    protected = list(manifest.get("protectedPatterns", []))
    invalid: list[str] = []
    for relative in sorted(changed_paths(root)):
        if relative == "workspace.json":
            continue
        if not matches(relative, managed) or matches(relative, protected):
            invalid.append(relative)
    if invalid:
        raise ValueError(
            "Update изменил неразрешённые пути: " + ", ".join(invalid)
        )


def validate_workspace_changes(original: Path, updated: Path) -> None:
    """Разрешает менять только template.version и служебный блок updates."""

    before = load_json(original)
    after = load_json(updated)
    before_safe = copy.deepcopy(before)
    after_safe = copy.deepcopy(after)
    before_safe.pop("updates", None)
    after_safe.pop("updates", None)
    before_safe.setdefault("template", {}).pop("version", None)
    after_safe.setdefault("template", {}).pop("version", None)
    if before_safe != after_safe:
        raise ValueError("Update попытался изменить личные поля workspace.json")


def record_pending_update(version: str, reason: str) -> None:
    state = load_json(STATE_PATH) if STATE_PATH.exists() else {}
    state.update(
        {
            "pending": True,
            "latestVersion": version,
            "pendingReason": reason,
            "lastCheckedAt": utc_now().isoformat().replace("+00:00", "Z"),
        }
    )
    write_json_private(STATE_PATH, state)


def private_origin_confirmed(root: Path) -> bool:
    remote = git("remote", "get-url", "origin", cwd=root, check=False)
    if remote.returncode != 0 or not remote.stdout.strip():
        return False
    try:
        result = subprocess.run(
            ["gh", "repo", "view", "--json", "visibility", "--jq", ".visibility"],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0 and result.stdout.strip().upper() == "PRIVATE"


def cleanup_worktree(worktree: Path, branch: str) -> None:
    git("worktree", "remove", "--force", str(worktree), cwd=ROOT, check=False)
    git("branch", "-D", branch, cwd=ROOT, check=False)


def push_private_origin(branch: str) -> bool:
    """Публикует только в подтверждённый private origin и допускает будущий retry."""

    if not private_origin_confirmed(ROOT):
        print("Push пока пропущен: origin не подтверждён как private.")
        return False
    pushed = git("push", "origin", branch, cwd=ROOT, check=False)
    if pushed.returncode != 0:
        print("Локальное обновление завершено, но push временно не удался.")
        return False
    return True


def auto_update(target: str | None) -> int:
    """Оркестрирует безопасное обновление без изменения исходного checkout."""

    workspace = read_workspace()
    updates = workspace.get("updates", {})
    if updates.get("autoApply", True) is not True:
        print("Автоматическое применение выключено.")
        return EXIT_USER_ACTION

    source = workspace["template"]["source"]
    current = plain_version(workspace["template"]["version"])
    target_version = plain_version(target or latest_release(source)["tag_name"])
    if parse_version(target_version) <= parse_version(current):
        state = load_json(STATE_PATH) if STATE_PATH.exists() else {}
        if updates.get("autoPush", True) is True and state.get("pushPending") is True:
            branch = git("branch", "--show-current", cwd=ROOT).stdout.strip()
            if branch and push_private_origin(branch):
                state["pushPending"] = False
                write_json_private(STATE_PATH, state)
                print("Ожидавший push успешно завершён.")
            else:
                print("Ожидавший push будет повторён при следующей проверке.")
            return 0
        print(f"Обновление не требуется: {current}")
        return 0

    dirty = git("status", "--porcelain", cwd=ROOT).stdout.strip()
    if dirty:
        record_pending_update(target_version, "working-tree-not-clean")
        print("Обновление отложено до следующего чистого старта.")
        return 0

    original_branch = git("branch", "--show-current", cwd=ROOT).stdout.strip()
    if not original_branch:
        print("Автоматическое обновление требует обычную локальную ветку.")
        return EXIT_USER_ACTION

    branch = f"codex/template-update-v{target_version}"
    worktree = ROOT / ".local/template-update-worktrees" / f"v{target_version}"
    if worktree.exists() or git("show-ref", "--verify", f"refs/heads/{branch}", cwd=ROOT, check=False).returncode == 0:
        print(f"Обнаружено незавершённое обновление: {branch}")
        return EXIT_USER_ACTION

    worktree.parent.mkdir(parents=True, exist_ok=True)
    created = git("worktree", "add", "-b", branch, str(worktree), "HEAD", cwd=ROOT, check=False)
    if created.returncode != 0:
        print(created.stderr.strip() or created.stdout.strip())
        return EXIT_VALIDATION

    rollback = updates.get("rollbackOnFailure", True) is True
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--apply",
        "--target",
        target_version,
    ]
    child_environment = os.environ.copy()
    child_environment["PERSONAL_CODEX_WORKSPACE_ROOT"] = str(worktree)
    applied = subprocess.run(command, cwd=worktree, env=child_environment, check=False)
    if applied.returncode != 0:
        if applied.returncode == EXIT_CONFLICT:
            print(f"Конфликты оставлены для агента в {worktree}")
            return EXIT_CONFLICT
        if rollback:
            cleanup_worktree(worktree, branch)
        return applied.returncode

    manifest = load_json(worktree / "template-manifest.json")
    try:
        validate_changed_paths(worktree, manifest)
        validate_workspace_changes(ROOT / "workspace.json", worktree / "workspace.json")
    except ValueError as exc:
        print(exc)
        if rollback:
            cleanup_worktree(worktree, branch)
        return EXIT_VALIDATION

    if updates.get("autoCommit", True) is not True:
        print(f"Обновление подготовлено без коммита: {worktree}")
        return EXIT_USER_ACTION

    git("add", "--all", cwd=worktree)
    checked = git("diff", "--cached", "--check", cwd=worktree, check=False)
    if checked.returncode != 0:
        print(checked.stdout + checked.stderr)
        if rollback:
            cleanup_worktree(worktree, branch)
        return EXIT_VALIDATION
    committed = git(
        "commit",
        "-m",
        f"Update Personal Codex Workspace to {target_version}",
        cwd=worktree,
        check=False,
    )
    if committed.returncode != 0:
        print(committed.stderr.strip() or committed.stdout.strip())
        if rollback:
            cleanup_worktree(worktree, branch)
        return EXIT_VALIDATION

    merged = git("merge", "--ff-only", branch, cwd=ROOT, check=False)
    if merged.returncode != 0:
        print("Исходная ветка изменилась; update worktree сохранён для повторной проверки.")
        return EXIT_CONFLICT

    cleanup_worktree(worktree, branch)

    state = load_json(STATE_PATH) if STATE_PATH.exists() else {}
    state.update(
        {
            "pending": False,
            "installedVersion": target_version,
            "lastAppliedAt": utc_now().isoformat().replace("+00:00", "Z"),
            "pushPending": False,
        }
    )

    if updates.get("autoPush", True) is True:
        state["pushPending"] = not push_private_origin(original_branch)
    write_json_private(STATE_PATH, state)
    print(f"Автоматическое обновление завершено: {current} -> {target_version}")
    return 0


def finalize(manifest: dict) -> int:
    conflict_marker = re.compile(r"^(?:<<<<<<<|\|\|\|\|\|\|\||=======|>>>>>>>)", re.MULTILINE)
    for relative in collect_managed(ROOT, manifest):
        path = ROOT / relative
        if not path.is_file() or path.suffix not in {".md", ".json", ".yml", ".yaml", ".py", ".txt"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if conflict_marker.search(text):
            print(f"Остались conflict markers: {relative}")
            return EXIT_CONFLICT

    try:
        run_commands(list(manifest.get("migrationCommands", [])), "Миграция")
        update_workspace_version(manifest["version"])
        run_commands(list(manifest.get("validationCommands", [])), "Проверка")
    except (RuntimeError, ValueError) as exc:
        print(f"Финализация не прошла: {exc}")
        return EXIT_VALIDATION
    return 0


def apply_update(target: str | None) -> int:
    workspace = read_workspace()
    if workspace.get("updates", {}).get("autoApply", True) is not True:
        print("Автоматическое применение выключено.")
        return EXIT_USER_ACTION
    ensure_clean_worktree()

    source = workspace["template"]["source"]
    current = plain_version(workspace["template"]["version"])
    target_version = plain_version(target or latest_release(source)["tag_name"])
    if parse_version(target_version) <= parse_version(current):
        print(f"Обновление не требуется: {current}")
        return 0

    with tempfile.TemporaryDirectory(prefix="template-release-") as temp:
        directory = Path(temp)
        base_root = download_tag(source, f"v{current}", directory / "base")
        target_root = download_tag(source, f"v{target_version}", directory / "target")
        manifest = load_json(target_root / "template-manifest.json")
        verify_release(target_root, manifest, target_version)

        if manifest.get("updateMode") != "automatic" or manifest.get("requiresUserAction") is True:
            print("Release manifest требует участия пользователя.")
            return EXIT_USER_ACTION
        if parse_version(UPDATER_VERSION) < parse_version(manifest["minimumUpdaterVersion"]):
            print("Текущая версия updater слишком старая.")
            return EXIT_USER_ACTION

        conflicts = apply_files(base_root, target_root, manifest)
        if conflicts:
            print("Требуется разрешить конфликты в update worktree:")
            for relative in conflicts:
                print(f"- {relative}")
            return EXIT_CONFLICT
        return finalize(manifest)


def finalize_current() -> int:
    if not MANIFEST_PATH.exists():
        print("template-manifest.json отсутствует")
        return EXIT_VALIDATION
    return finalize(load_json(MANIFEST_PATH))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true")
    action.add_argument("--if-due", action="store_true")
    action.add_argument("--apply", action="store_true")
    action.add_argument("--auto", action="store_true")
    action.add_argument("--finalize", action="store_true")
    parser.add_argument("--target", help="Целевая версия без обязательного префикса v")
    args = parser.parse_args()

    try:
        if args.check or args.if_due:
            return check_update(if_due=args.if_due)
        if args.apply:
            return apply_update(args.target)
        if args.auto:
            return auto_update(args.target)
        return finalize_current()
    except (OSError, ValueError, KeyError, urllib.error.URLError, RuntimeError) as exc:
        print(f"Обновление остановлено: {exc}")
        return EXIT_VALIDATION


if __name__ == "__main__":
    sys.exit(main())
