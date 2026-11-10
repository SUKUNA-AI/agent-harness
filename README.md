# Native Agent Harness 1.6.1

Локальный комплект для Codex: общие инструкции, память, работа с большими
репозиториями, делегация и проверяемое продолжение задач. Модели запускает
установленный Codex. Harness хранит состояние и связывает проверки с конкретными
исходниками. Дополнительный LLM API, ключ, сервер, daemon и база данных не нужны.

[Скачать полный ZIP 1.6.1](https://github.com/SUKUNA-AI/agent-harness/releases/download/v1.6.1/agent-harness-1.6.1.zip) ·
[Изменения выпуска](docs/RELEASE_NOTES_1.6.1.md) ·
[Подробная установка](SETUP_RU.md) · [Документация](docs/INDEX.md)

В 1.6.1 собраны исправления после 1.6.0: до **12 managed-сессий** и **3 reviewer**,
effort по роли и риску, Rust LSP на Windows, ограниченный SessionStart bootstrap,
проверка Python aliases и восстановление Windows slots/сессий.

## Что входит

| Возможность | Как использовать |
| --- | --- |
| Общие правила и роли | Установка в профиль Codex; новые чаты получают инструкции и skills |
| Native-сессия | SessionStart hook вызывает Memory, recall, Ledger inform и Context Reducer |
| Исполнители | Native tools Codex либо managed supervisor с отдельными worktrees |
| Автоматический цикл | `work start`: план → исполнение → проверки → review → gate → repair |
| Продолжение | Checkpoint, точный session ID, исходные attempts/deadlines и проверка evidence |
| Быстрые инструменты | Rust snapshot/RepoMap; LSP symbols, definitions, references и rename plan |
| Проверяемая поставка | Portable ZIP, manifest, SHA-256, native bundles и отчёт проверок |

Короткую задачу агент выполняет напрямую. Сохраняемый run нужен для длительной
работы, повторяемой приёмки или восстановления. Один writer работает в одном
checkout; независимые изменения Git выполняются в отдельных worktrees.

## Требования и поставка

- **Python 3.11+**. Основной runtime использует stdlib и включённый PyYAML 6.0.3;
  установка через pip для него не нужна.
- Установленный и авторизованный **локальный Codex** для модельных задач.
  Проверить файлы установки можно без model calls.
- **Linux x86_64/glibc или Windows x86_64** для готовых native bundles.
  Другие платформы этим выпуском не квалифицированы.
- Для LSP — отдельно установленный language server и зависимости проекта.
  Для read-only MCP — отдельный optional SDK `mcp==2.3.0`.

**Полный релизный ZIP** содержит `bin/linux-x86_64` и `bin/windows-x86_64`.
Git clone и автоматический GitHub «Source code ZIP» содержат исходники без
готовых бинарников. Для сборки helper из Git нужны Rust **1.90.0**, Cargo и
C toolchain; при использовании готового bundle они не нужны.

Скачайте ZIP и `SHA256SUMS` из [выпуска](https://github.com/SUKUNA-AI/agent-harness/releases/tag/v1.6.1).
Сравните SHA-256 архива с `SHA256SUMS`: Linux —
`sha256sum agent-harness-1.6.1.zip`, PowerShell —
`Get-FileHash .\agent-harness-1.6.1.zip -Algorithm SHA256`.
Распакуйте пакет **в постоянный каталог**: профиль будет ссылаться на его файлы.
Состав, source revision и проверки указаны в `release-manifest.json` и
`VERIFICATION.json`. Не подменяйте бинарник bundle файлом от другой версии.

## Установка в профиль Codex

Установка действует для всех новых чатов **выбранного локального профиля**.
Другой пользователь ОС, `CODEX_HOME`, WSL или удалённый компьютер настраиваются
отдельно. Открытые чаты автоматически не перечитывают пакет.

Примеры ниже намеренно выбирают **12 native children**. Без явного
`--max-child-threads` переносимый default — 5; ранее сохранённый более строгий
предел сохраняется. Это отдельная настройка от лимитов managed supervisor.

### Linux

Выполняйте команды из распакованного package root. Замените путь в `cd`:

```bash
cd "$HOME/tools/agent-harness-1.6.1"
HARNESS_HOME="${CODEX_HOME:-$HOME/.codex}"
HARNESS_SKILLS="$HOME/.agents/skills"
HARNESS_STATE="${XDG_STATE_HOME:-$HOME/.local/state}/agent-harness"

python3 tools/harness.py doctor --codex-home "$HARNESS_HOME" --skills-home "$HARNESS_SKILLS" --state-dir "$HARNESS_STATE"
python3 tools/harness.py install --codex-home "$HARNESS_HOME" --skills-home "$HARNESS_SKILLS" --state-dir "$HARNESS_STATE" --max-child-threads 12
```

Второй вызов показывает **dry-run**. Просмотрите diff и выбранные пути. Затем:

```bash
python3 tools/harness.py install --codex-home "$HARNESS_HOME" --skills-home "$HARNESS_SKILLS" --state-dir "$HARNESS_STATE" --max-child-threads 12 --apply
python3 tools/harness.py verify --codex-home "$HARNESS_HOME" --skills-home "$HARNESS_SKILLS" --state-dir "$HARNESS_STATE"
python3 tools/harness.py install --codex-home "$HARNESS_HOME" --skills-home "$HARNESS_SKILLS" --state-dir "$HARNESS_STATE" --max-child-threads 12 --apply
```

### Windows PowerShell

Используйте Python 3.11+; если `py -3` выбирает другую версию, укажите нужный
Python executable. Команды выполняются из постоянного package root:

```powershell
Set-Location (Join-Path $HOME "Tools\agent-harness-1.6.1")
$HarnessHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }
$HarnessSkills = Join-Path $HOME ".agents\skills"
$HarnessState = Join-Path $HOME ".local\state\agent-harness"

py -3 tools/harness.py doctor --codex-home "$HarnessHome" --skills-home "$HarnessSkills" --state-dir "$HarnessState"
py -3 tools/harness.py install --codex-home "$HarnessHome" --skills-home "$HarnessSkills" --state-dir "$HarnessState" --max-child-threads 12
# После просмотра dry-run:
py -3 tools/harness.py install --codex-home "$HarnessHome" --skills-home "$HarnessSkills" --state-dir "$HarnessState" --max-child-threads 12 --apply
py -3 tools/harness.py verify --codex-home "$HarnessHome" --skills-home "$HarnessSkills" --state-dir "$HarnessState"
py -3 tools/harness.py install --codex-home "$HarnessHome" --skills-home "$HarnessSkills" --state-dir "$HarnessState" --max-child-threads 12 --apply
```

При ненулевом exit code остановитесь и разберите JSON-отчёт; PowerShell хранит
код внешней команды в `$LASTEXITCODE`. Ожидаются `structural_verified` у verify
и `unchanged` у последнего повторного install.

### Подключение в клиенте

Установщик добавляет managed block в активный глобальный `AGENTS.md` или
`AGENTS.override.md`, три роли, skills, machine pointer и SessionStart hook.
Существующие инструкции, чужие handlers и настройки модели сохраняются.

Откройте новый процесс Codex, проверьте точный handler через `/hooks` и доверьте
его штатным интерфейсом клиента. Установщик не обходит trust. Подтвердите
SessionStart новым стартом сессии. В отдельном пустом каталоге можно выполнить:

```bash
# Linux; используйте пути профиля из предыдущих шагов.
HARNESS_PROBE="$(mktemp -d)"
python3 tools/harness.py verify --codex-home "$HARNESS_HOME" --skills-home "$HARNESS_SKILLS" --state-dir "$HARNESS_STATE" --native --cwd "$HARNESS_PROBE"
```

На Windows тот же `verify --native` запускается через `py -3` с существующим
пустым `--cwd`. `native_input_verified` подтверждает поддержанные CLI-probes и
policy во входе модели; фактический SessionStart callback проверяется отдельно.
Порядок, конфликт hooks и rollback описаны в [SETUP_RU](SETUP_RU.md).

## Работа с задачами

После установки можно описать задачу обычным сообщением Codex. Агент выбирает
нужные инструменты, распределяет независимые части и проверяет итог.
Пользователю не нужно вручную создавать supervisor run для каждого запроса.

Для управляемого цикла подготовьте **private contract** с целью, разрешёнными
write scopes, executable acceptance checks и бюджетами. Пример contract и
пошаговый tutorial — [Execution Supervisor](docs/EXECUTION_SUPERVISOR_RU.md).
Для managed memory явно задайте `memory: true`, `ledger: inform`:
upstream default памяти — `false`.

Команды из package root; замените абсолютные пути. На Windows используйте
`py -3` и пути Windows:

```text
python tools/harness.py work start --contract /private/task.yaml --cwd /project/repo --run-dir /private/run
python tools/harness.py work status --run-dir /private/run --summary --verify-evidence
python tools/harness.py work watch --run-dir /private/run --timeout 60
python tools/harness.py work report --run-dir /private/run --format yaml
python tools/harness.py work stop --run-dir /private/run --reason "pause requested"
python tools/harness.py work resume --run-dir /private/run
```

`start` и `resume` работают в переднем плане. Status/watch можно открыть во
втором терминале; watch читает локальное состояние без model calls. После
остановки foreground процесса продолжение требует `work resume`. Исправление
использует точный session ID и прежние attempts/deadlines; restart их не повышает.
Неизвестный исход записи сначала требует reconciliation.

| Режим и лимит | Значение |
| --- | --- |
| Native children Codex | `agents.max_concurrent_threads_per_session`; root не считается; default 5, явный пример установки — 12 |
| Managed sessions | До 12 одновременно на общий state root, включая planner, reviewer и integration owner; меньший contract cap сохраняется |
| Managed reviewers | До 3 одновременно во всех runs одного state root; остальные слоты доступны другим ролям |
| Managed concurrency >1 | Требует свежий binary/version-bound proof подавления native spawn; без proof используйте native/single вариант |

Native управляет дочерними агентами средствами Codex. Managed запускает
отдельные `codex exec/resume`, подавляет рекурсивный native spawn и интегрирует
изменения в staging worktree. Report указывает фактический accepted candidate;
исходный checkout пользователя сохраняется. Подробнее —
[Managed agents](docs/DYNAMIC_AGENTS_RU.md).

Успех модели не означает `ACCEPTED`. Gate проверяет текущий candidate,
обязательные checks и предусмотренные контрактом coverage/review. `unknown`,
`not_run` и `skipped` не считаются пройденными проверками.

## Настройки и effort

Установщик сохраняет model/provider, усилие координатора, разрешения и
безопасность. Добавляет отсутствующие agent defaults и порог compaction
`500000`; явное пользовательское значение не заменяется. Этот порог не
увеличивает context window модели.

- Native-роли: `delivery_owner` и `delivery_reviewer` — `high`, helper — `medium`.
  Role TOML влияет на фактический effort независимо от текста брифа.
- Managed owner/architect/diagnostician: `medium`, при `risk: high` — `high`.
- Managed reviewer: `high`, при `risk: high` — `ultra`; допустим явный per-node
  override в пределах значений роли. Planner использует `high`.

Effort передаётся конкретному managed-вызову; глобальная модель и effort
координатора сохраняются. Полная таблица наследования и legacy keys —
[Configuration](docs/CONFIGURATION_RU.md).

## Поиск, Rust и LSP

Начните с адресного `rg`. Для незнакомых связей используйте RepoMap, для
семантики символов — LSP. Из package root можно проверить инструменты на пакете:

```bash
python3 tools/harness.py native status
python3 tools/harness.py context map --root . --query "snapshot input freshness" --max-bytes 8192
python3 tools/harness.py context read --root . --file src/agent_harness/native.py --start-line 1 --lines 60 --max-bytes 8192
python3 tools/harness.py code doctor --root . --file src/agent_harness/native.py
python3 tools/harness.py code symbols --root . --file src/agent_harness/native.py
```

Snapshot выбирает `AGENT_HARNESS_BACKEND=auto|python|native`. В `auto` проверенный
Rust helper используется для любого размера дерева; без helper — Python.
RepoMap требует Rust. Обычный вызов не скачивает и не компилирует helper.

LSP отдельно выбирает `AGENT_HARNESS_LSP_BACKEND=auto|python|native`; default
`auto` предпочитает проверенный Rust transport на Linux/Windows. Linux требует
x86_64/glibc, `/proc` process identity и обычный SIGCHLD без auto-reaping.
Windows использует suspended launch → private Job → resume. Повреждённый bundle
или ошибка выбранного helper завершают вызов без повторного запуска через Python.
macOS/ARM transport не добавлен.

Language server устанавливается отдельно. Harness запускает его на время
команды и завершает после неё. Rename сначала создаёт план; отдельный `code apply`
сверяет исходные bytes перед записью. Rust ускоряет локальную работу с файлами и
transport; ускорение генерации модели этим не заявляется.
[RepoMap/context](docs/CONTEXT_AND_NATIVE_RU.md) · [LSP](docs/LSP_RU.md).

## Память, контекст и восстановление

Native SessionStart вызывает реальные Memory, recall, Ledger inform и reducer
без второй модели. Короткий packet содержит session ID, pointers, provenance и
явные omissions. Bootstrap читает ограниченную цепочку instruction-файлов
предков cwd, не обходит домашнее дерево. Это `bounded_context`, а не полная
проверка source inventory или acceptance.

После существенного этапа сохраняется private checkpoint с evidence digest и
следующим шагом. Для явно контролируемых вызовов Ledger govern даёт advisory;
он не перехватывает все tools Codex и не расширяет разрешения. Required checks
выполняются заново; память не превращает старое `passed` в текущую приёмку.

Для продолжения нужны исходный package, private state, manifests и сохранённые
receipts. Не коммитьте состояние, prompt, transcripts, токены и ключи в проект.
Сжатие истории остаётся механизмом Codex; hot reload и продолжение после
закрытия приложения не обещаются.
[Memory/context](docs/MEMORY_AND_CONTEXT_RU.md) ·
[Native sessions](docs/NATIVE_SESSION_RU.md) · [Recovery](docs/OPERATIONS.md).

## Обновление и диагностика

Распакуйте новую полную версию в постоянный каталог и повторите
**doctor → install dry-run → apply → verify → повторный install (`unchanged`)**
с прежними Codex home, skills home и private state. Затем проверьте новый
процесс Codex. Git push и публикация релиза сами профиль не обновляют.
Сохраните прежний пакет и private state для rollback по
[инструкции](SETUP_RU.md).

Для source checkout обновите исходники и явно пересоберите native helper:

```bash
python3 tools/build_native.py
python3 tools/check_native.py
```

Старый ignored `bin/` не становится совместимым после `git pull`: version,
source digest и binary hash должны совпадать.

| Симптом | Следующий шаг |
| --- | --- |
| `Hook failed`, exit 2 | Проверьте выбранный профиль, stderr, команду `/hooks`, постоянный package path и Python launcher; [диагностика](docs/AUTOLOAD_RU.md) |
| `verify` сообщает canonical diff | Сверьте package root и owned files с dry-run; сохраните ручные изменения, не перезаписывайте конфликт вслепую |
| Нет Rust helper | Возьмите полный ZIP или явно соберите helper; проверьте `native status` |
| LSP не находит symbols | Проверьте `code doctor`, language server и зависимости проекта |
| `recovery_required` | Сохраните receipts/backups, выясните исход незавершённой операции и продолжайте по [Recovery](docs/EXECUTION_SUPERVISOR_RU.md) |

## Разработка и подробная документация

```bash
python tools/release_check.py
python tools/run_tests.py
cargo test --release --manifest-path native/Cargo.toml --locked
python tools/build_native.py
python tools/check_native.py
python tools/build_release.py --output-dir /absolute/release-output
```

Сборщик требует чистый committed candidate и проверенные bundles. Полный ZIP
проверяется после извлечения; isolated install/verify/reinstall/rollback не
подменяют реальную проверку клиентского профиля или model benchmark. Результаты
и ограничения конкретного выпуска находятся в его `VERIFICATION.json`.

[CLI](docs/CLI_REFERENCE_RU.md) · [Архитектура](docs/ARCHITECTURE_RU.md) ·
[Схемы SVG/draw.io](docs/diagrams/README.md) · [Приёмка](docs/ACCEPTANCE.md) ·
[Форматы и миграция](docs/STATE_FORMATS_AND_MIGRATION_RU.md) ·
[Интеграции](integrations/README.md) · [История изменений](CHANGELOG.md)
