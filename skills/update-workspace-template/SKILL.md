---
name: update-workspace-template
description: "Автоматически обновляет приватную копию Personal Codex Workspace до последнего совместимого stable release: проверяет центральный источник и manifest/checksums, применяет изменения и миграции в отдельном Git worktree, разрешает безопасные конфликты, запускает проверки, коммитит, сливает и отправляет результат в private origin. Использовать при коде 10 от template_update.py, прямой просьбе обновить шаблон или однократном bootstrap копии до 1.3.0."
---

# Автоматическое обновление шаблона

## Проверить разрешение и источник

1. Прочитать `workspace.json`, `template-manifest.json` и `AGENTS.md`.
2. Использовать только exact `workspace.template.source` и stable release.
3. При `updates.autoApply=false` показать план и остановиться до разрешения.
4. SemVer не является stop point: следовать `updateMode` и `requiresUserAction` release manifest.
5. При `requiresUserAction=true`, неподтверждённом tag/source/checksum или security warning остановиться.

## Не затрагивать текущую работу

Основной путь – выполнить `python3 scripts/template_update.py --auto`. Команда сама:

1. Проверяет `git status`; грязное дерево записывается как pending update до следующего чистого старта.
2. Создаёт ветку `codex/template-update-v<VERSION>` и ignored worktree.
3. Применяет release, миграции и проверки.
4. Проверяет, что diff содержит только manifest-managed пути и служебное поле `workspace.json`.
5. Коммитит, fast-forward-сливает и push-ит только подтверждённый private origin.

Для bootstrap копии до 1.3.0 сначала клонировать exact stable tag в ignored
`.local/template-bootstrap/`, прочитать его skill, manifest и updater, затем из
корня приватной копии выполнить:

```bash
PERSONAL_CODEX_WORKSPACE_ROOT="$PWD" \
  python3 .local/template-bootstrap/scripts/template_update.py --auto
```

Не копировать updater в tracked-пути старой копии: внешний root позволяет ему
создать чистый update-worktree и установить собственные файлы как часть release.

## Применить в worktree

1. При ручном восстановлении автоматического процесса запустить `python3 scripts/template_update.py --apply` внутри update worktree.
2. Скрипт скачивает base tag и latest release, проверяет SHA-256, применяет управляемые файлы и обновляет только `workspace.template.version` в защищённом `workspace.json`.
3. При текстовых конфликтах самостоятельно разрешить только однозначные различия, сохраняя локальный смысл и новую защитную логику.
4. После разрешения выполнить `python3 scripts/template_update.py --finalize`.
5. Если конфликт затрагивает личные данные или не имеет однозначного безопасного решения, удалить worktree/ветку и оставить прежнюю версию.

## Проверить и интегрировать

1. Выполнить validation commands из manifest и privacy scan.
2. Просмотреть diff, список файлов, binaries и отсутствие protected paths.
3. При `autoCommit=true` создать один update-коммит.
4. Вернуться в исходный checkout и выполнить fast-forward merge update-ветки. Если исходная ветка успела измениться, повторно перенести update на её новый HEAD и прогнать проверки.
5. При `autoPush=true` проверить, что origin действительно private, затем push текущей ветки.
6. Удалить временный worktree и update-ветку после успешного merge.

Не создавать private PR для обычного успешного обновления. Остановиться только при unsafe state, неуспешных проверках или ручной миграции. В результате кратко сообщить старую и новую версии, commit, push и выполненные миграции.
