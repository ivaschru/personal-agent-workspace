# Однократное подключение автоматических обновлений

Копии, созданные до версии 1.3.0, ещё не содержат updater и не могут обнаружить его самостоятельно. В такой копии достаточно один раз сказать агенту:

```text
Установи механизм автоматических обновлений из последнего stable release
https://github.com/ivaschru/personal-codex-workspace,
не затрагивая мои личные данные, задачи и настройки.
```

Агент должен:

1. Проверить exact центральный репозиторий и latest release.
2. Клонировать release в ignored `.local/template-bootstrap/`.
3. Прочитать `skills/update-workspace-template/SKILL.md`, updater и release manifest из центральной копии.
4. Сравнить текущую версию из `workspace.json.template.version` с release manifest.
5. Из корня приватной копии запустить проверенный updater нового release, явно передав ему корень старой копии:

   ```bash
   PERSONAL_CODEX_WORKSPACE_ROOT="$PWD" \
     python3 .local/template-bootstrap/scripts/template_update.py --auto
   ```

6. Не выполнять произвольный remote script без предварительного чтения source, tag и manifest/checksums.

Внешний updater создаст update-worktree от текущей приватной ветки. В основной checkout он попадёт только после успешных миграций, тестов и проверки защищённых путей.

После успешного перехода на 1.3.0 дальнейшие проверки и обновления выполняются автоматически.
