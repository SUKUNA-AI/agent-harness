# Архитектура Native Agent Harness 1.7.0

[Индекс](INDEX.md) · [README](../README.md) · [CLI](CLI_REFERENCE_RU.md)

Harness сочетает штатное исполнение Codex с локальными инструментами для
поиска, проверок и восстановления состояния. Разделение позволяет адресно
читать код и проверять связь результата с входами; ускорение требует измерения.
Файловое состояние находится в явно выбранном private store; общая служба
и БД отсутствуют.

Документ описывает исходники после 1.7.0. Опубликованный
[ZIP 1.7.0](RELEASE_NOTES_1.7.0.md) закреплён своим тегом и manifest;
подготовленные изменения перечислены в [следующей поставке](RELEASE_NOTES_NEXT.md).

## 1. Кто исполняет работу

Foreground control plane включает `supervisor.py`, adapters в
`codex_execution.py`/`opencode_execution.py`, durable `supervisor_store.py`
и `strict_yaml.py`.
Native SessionStart работает отдельно через `native_session.py` и не
запускает supervisor. Для managed contract v2 `plan_quality.py` проверяет
план до worker dispatch; node и final acceptance связываются с одним candidate.
Существующие `core.Run` и `runner` остаются механизмом snapshot/evidence/gate.
Модельный inference выполняет выбранный штатный клиент: Codex по умолчанию,
опционально OpenCode V2 в managed leaf mode. Полный FSM и diagram:
[Execution Supervisor](EXECUTION_SUPERVISOR_RU.md). Ledger/memory/context:
[Memory](MEMORY_AND_CONTEXT_RU.md); DAG/worktrees: [Dynamic](DYNAMIC_AGENTS_RU.md).

![Клиенты, supervisor, локальные инструменты и граница model provider](diagrams/runtime-architecture.svg)

[Редактировать в draw.io](diagrams/runtime-architecture.drawio) · [Все схемы](diagrams/README.md)

<details>
<summary>Mermaid source той же схемы</summary>

```mermaid
flowchart TD
    User["Пользователь<br/>Цель · scope · действующие разрешения"]
    Codex["Codex — backend по умолчанию<br/>Исходный native-режим сохранён<br/>Native tools / shell / delegation<br/>Managed exec / exact-session resume<br/>Strict transport — отдельный opt-in<br/>Tool Profiles выбираются отдельно"]
    Work["Supervisor managed flow<br/>DAG · scopes · отдельные worktrees<br/>До 12 sessions, включая до 3 reviewers<br/>Checkpoints · retry · общие budgets"]
    Exec["OpenCode 2.2.5 — optional managed leaf<br/>Native permissions + runtime guard<br/>--standalone: private owned server<br/>OpenCode-native subagents: NOT_IMPLEMENTED"]
    Profiles["Tool Profiles — optional policy<br/>Baseline без внешних dependencies<br/>Выбранный профиль → native config<br/>Role permissions + catalog audit"]
    State["Private локальное состояние<br/>Journal · typed receipts · checkpoints<br/>Memory recall + Ledger inform + reducer<br/>Без новой БД или обязательного daemon"]
    Provider["OpenRouter — provider модели OpenCode<br/>Hydrogen/Flex, DeepSeek FP8: no fallback<br/>Opus 5.5: без route pin<br/>Auth хранится в штатном клиенте"]
    MCP["MCP на пользовательском host<br/>Serena 2.1.0: сервер/cache на worktree<br/>Context7 4.6.0: public docs reads<br/>Playwright MCP 0.0.85: конечный запуск<br/>Exact loopback origins · private artifacts"]
    CLI["Локальные CLI и source tools<br/>Graphify graphifyy 0.9.111: frozen index<br/>Commit + dirty bytes + config/tool digests<br/>ast-grep 0.51.3: before/rewrite/diff/after/tests<br/>RepoMap · LSP · source packets"]
    Managed["Isolated managed workers<br/>Один writer на worktree<br/>Client/profile/source/index binding<br/>Собственные MCP connections/cache<br/>Checks запускает supervisor"]
    Candidate["Общий candidate и source-bound acceptance<br/>Integration → freeze → independent checks / requirement coverage<br/>Fresh independent review → final gate; MCP success не означает ACCEPTED"]
    Limits["Границы квалификации<br/>Strict Codex transport EXPERIMENTAL: 0.167.0 + scripted provider; paid workload UNQUALIFIED.<br/>OpenCode: подтверждён конкретный Hydrogen + Serena + Context7 smoke; новый run требует fresh proof.<br/>Docling исключён и не установлен. Схема не подтверждает приёмку новой задачи или публикацию релиза."]
    User -->|"native"| Codex
    User -->|"managed"| Work
    Work --> Codex
    Work --> Exec
    Codex -->|"profile selected"| Profiles
    Work -->|"profile selection"| Profiles
    Work -->|"receipts"| State
    Exec -->|"native model request"| Provider
    Profiles -->|"native client connects"| MCP
    Profiles -->|"local commands"| CLI
    Work --> Managed
    CLI -->|"checks / provenance"| Candidate
    Managed -->|"scoped integration"| Candidate
    Codex --> Candidate
```

</details>

| Слой | Ответственность | Что он не подтверждает сам по себе |
| --- | --- | --- |
| Codex | Размышление, действия инструментами, native agents, работа в пользовательских permissions | Правильность результата без наблюдений |
| Bootstrap/skills/workflows | Направление агента к подходящему процессу и источникам | Обязательное исполнение каждой инструкции конкретной моделью |
| Python control plane | Валидация contracts/plans, command runner, receipts, gate, установка и LSP adapter | Полноту намерения пользователя или выдачу новых permissions |
| Rust helper | Inventory, metadata, content hashes, canonical snapshot и ограниченная карта символов | Полную семантику языка и ускорение model inference |
| Language server | Семантику проекта в пределах своей реализации и настроек | Полноту неиндексированного/неподдерживаемого кода |
| Private files | Состояние run, checkpoints и recovery evidence | Автоматическое продолжение без активного клиента |

CLI не содержит дополнительного LLM API client. `plan next` показывает ready
nodes для native orchestration; `work` запускает штатные client processes,
владеет global slots и отдельными worktrees. Codex exec/resume остаётся default.
Optional OpenCode V2 adapter использует тот же supervisor и contained runner;
модельный transport остаётся внутри выбранного клиента.

OpenRouter является provider модели для OpenCode. Его native SDK и auth store
обрабатывают model requests и выбранный route; harness не получает новый LLM
API client. Serena, Context7 и Playwright подключаются к локальному CLI как MCP.
Graphify и ast-grep выполняются как локальные CLI; они не становятся MCP от
наличия Tool Profile. Инструменты не устанавливаются на OpenRouter.

Переносимый пакет предоставляет `tools` и `work` в существующем CLI. Имена
machine-specific wrappers и пользовательские shortcuts не являются новыми
командами пакета и не гарантируют наличие MCP в обычном интерактивном клиенте.
Настройка файлов действует при поддержанном новом запуске; hot reload
текущей беседы и OpenCode-native orchestration не заявляются.

Tooling pipeline отделяет Strict YAML policy → native configuration → dependency
probe → actual client catalog → execution qualification → completion → gate.
[Подробный операторский flow](TOOLING_OPERATIONS_RU.md) содержит
[схему конфигурации](diagrams/tool-profile-pipeline.svg) и
[схему isolation/resume/acceptance](diagrams/tooling-isolation-resume.svg).
Номер версии work-state v4 относится к tooling provenance; он не заменяет
contract v2 plan-quality или frozen receipt schemas v1/v2.

### Связь с исходниками

| Контракт | Основные файлы |
| --- | --- |
| Запуск CLI | `tools/harness.py`, `src/agent_harness/cli.py` |
| Контракт, state, snapshot, gate | `core.py`, `validation.py`, `runner.py` |
| Совместимые native plans и briefs | `orchestration.py` |
| Foreground edit/repair и managed DAG | `supervisor.py`, `work_contract.py`, `scheduling.py`, `managed_execution.py` |
| Проверка плана и требований | `plan_quality.py`, `verified_flow.py`, `completion_eligibility.py` |
| Подготовка root-вызова и восстановление | `root_preparation.py`, `execution_receipts.py`, `dependency_commit.py` |
| SessionStart, checkpoint и native observations | `native_session.py`, `native_observability.py` |
| Exact sessions и owned processes/worktrees | `codex_execution.py`, `agent_lifecycle.py` |
| Optional native backend и роль/профиль | `execution.py`, `opencode_execution.py`, `tool_profiles.py` |
| Strict Codex owned stdio execution | `codex_app_server.py`, `codex_qualification.py`, `codex_policy_probe.py`; legacy `codex_execution.py` сохраняется |
| Dependency и native catalog qualification | `tooling_probe.py`, `codex_tool_catalog.py`, `opencode_qualification.py` |
| Frozen graph, scoped migration и tooling formats | `tooling_provenance.py`, `tooling_state.py`, `execution_receipts.py` |
| Native optional launchers и local Graphify builder | `local_tool_integrations.py` |
| Finite managed browser MCP и independent regression | `browser_integration.py`, `optional_tool_jobs.py` |
| Strict YAML, journal и replay | `strict_yaml.py`, `supervisor_store.py` |
| Provenance, память и reducer | `execution_ledger.py`, `memory.py`, `context_reducer.py` |
| Optional conservative reuse | `input_closure.py`, `check_cache.py`, `python_runtime.py` |
| Engineering queries и measured serialization | `repomap_queries.py`, `format_bench.py` |
| Ограниченная выдача | `context.py`, `presentation.py`, `repo_map.py` |
| Выбор native backend | `native.py`, `native_cli.py` |
| Rust helper | `native/src/`, `native/Cargo.lock` |
| LSP и транзакция rename | `lsp.py`, `rename.py` |
| Установка и наблюдение настроек | `installation.py`, `diagnostics.py`, `file_io.py` |

Пути без префикса в таблице относятся к `src/agent_harness/`.

Logical profile и resolved execution profile хранятся раздельно: первый задаёт
исходную policy identity, второй раскрывает canonical worktree/cache bindings
и dynamic browser dependency. Это позволяет сохранить original digest и
одновременно проверить конкретную native конфигурацию. Lazy resolution не
скачивает зависимости и не включает Context7 в coding/architecture без явного
выбора `*-context7` варианта.

Serena/Context7 launcher запускает исходный server с private HOME/XDG и
implementation hash. Graphify builder читает frozen copy и связывает real AST
artifact с source/toolchain. Browser facade ограничивает official MCP finite
actions и exact numeric loopback HTTP origins; arbitrary code/upload tools
не предоставляет. Browser artifacts плюс independent regression подтверждают
конкретный job. Client qualification и source-bound final gate остаются
следующими отдельными границами.

На Windows root binding использует полный `VolumeSerialNumber` и opaque
128-bit `FileId`, без fallback на усечённую `stat` identity. Перед чтением
проверяются ancestors и reparse boundaries; сравнение aliases выполняется
по физической идентичности. Совпадение строк пути не подменяет ownership и
source proof. Python и Rust используют согласованную snapshot projection.

## 2. Как данные попадают в контекст

![Выбор источника контекста](diagrams/context-routing.svg)

[Редактировать в draw.io](diagrams/context-routing.drawio) · [Все схемы](diagrams/README.md)

<details>
<summary>Mermaid source той же схемы</summary>

```mermaid
flowchart TD
    Need["Вопрос по коду"] --> Exact{"Известно точное имя или место?"}
    Exact -->|"Да"| Search["rg: узкий поиск"]
    Exact -->|"Нет"| Map["RepoMap: граф файлов и ranking по задаче"]
    Search --> Choose["Выбрать нужные файлы и диапазоны"]
    Map --> Choose
    Choose --> Semantic{"Нужна семантика символа?"}
    Semantic -->|"Да"| Language["LSP: symbols, definition, references"]
    Semantic -->|"Нет"| Read["context read: ограниченная выдержка"]
    Language --> Read
    Read --> Use{"Кому нужны входы?"}
    Use -->|"Текущему агенту"| Work["Работа по paths, ranges и hashes"]
    Use -->|"Исполнителю или reviewer"| Pack["context pack: явный scope и omissions"]
    Pack --> Verify["context verify перед использованием"]
    Verify -->|"Актуален"| Work
    Verify -->|"Изменился"| Choose
```

</details>

RepoMap строит граф в памяти отдельного процесса. Синтаксические definitions и
references связывают файлы; ranking учитывает запрос и focus files. Пределы
числа файлов, bytes входа, bytes ответа и времени ограничивают стоимость
вызова. Результат карты помогает выбрать чтение и не заменяет точные
references языка. Неполная карта остаётся явно неполной.

Source packet — файл с выбранными исходниками и manifest. Он удобен для общего
контекста исполнителей, но не является shared mutable memory. При изменении
источника его пригодность нужно проверить. Excerpt хранит hash полного файла,
чтобы страницы можно было связать с одним содержимым. Ограничение вывода не
означает, что вычисление полного hash читает только показанные строки.

### Виды лимитов

| Лимит | Единица и назначение |
| --- | --- |
| `context map --max-bytes`, `context read --max-bytes` | Размер сериализованной JSON-выдачи в UTF-8 bytes |
| `context pack --max-bytes` | Бюджет исходного содержимого packet; envelope/manifest рассматриваются отдельно в CLI reference |
| `--max-files`, `--max-file-bytes`, `--max-total-bytes` | Ограничения входного объёма соответствующего инструмента |
| `status/handoff --max-output-bytes` | Размер краткого JSON-ответа |
| `tool_output_token_limit` Codex | Отдельный лимит retained tool output в native history |
| `model_auto_compact_token_limit` | Порог native compaction истории, выбранный пользователем |

Нельзя напрямую приравнивать 8192 bytes к 8192 tokens. Реальная стоимость
зависит от tokenizer, модели, дополнительных сообщений и поведения клиента.
Точные defaults и ограничения аргументов — в [CLI reference](CLI_REFERENCE_RU.md).

## 3. Где работает Rust и что сохраняет Python

![Выбор snapshot backend](diagrams/snapshot-backends.svg)

[Редактировать в draw.io](diagrams/snapshot-backends.drawio) · [Все схемы](diagrams/README.md)

<details>
<summary>Mermaid source той же схемы</summary>

```mermaid
flowchart TD
    Call["Запрос snapshot"]
    Mode{"AGENT_HARNESS_BACKEND"}
    Python["Python snapshot"]
    Required["Обязательный native helper"]
    Available{"Helper доступен и проверен?"}
    Native["Rust snapshot: любой размер"]
    Manifest["Полный content rehash и canonical manifest"]
    Binding["Digest inputs для checks, evidence и gate"]
    Error["Явная ошибка; без повторного запуска через Python"]
    Call --> Mode
    Mode -->|"python"| Python
    Mode -->|"native"| Required
    Mode -->|"auto"| Available
    Available -->|"Да"| Native
    Available -->|"Нет"| Python
    Available -->|"Invalid bundle"| Error
    Required -->|"Проверен"| Native
    Required -->|"Нет / ошибка"| Error
    Native -->|"Ошибка"| Error
    Native --> Manifest
    Python --> Manifest
    Manifest --> Binding
```

</details>

Наличие bundle проверяется по metadata/версии/протоколу и hash бинарника.
Повреждённый найденный helper не считается отсутствующим. Native timeouts и
protocol errors не превращаются в успешный Python fallback. `context map`
вызывает Rust независимо от выбора snapshot и требует helper.

Процессный cache хранит проверку неизменного установленного бинарника. Snapshot исходников каждый раз проверяет содержимое.
Оба backend должны возвращать одинаковый canonical manifest: это проверяет
`tools/check_native.py`, включая scope/ignore semantics и нестандартные paths.

Python сохранён там, где важнее явные state transitions и переносимая stdlib:
installer, contracts, subprocess runner, evidence/gate, bounded summaries,
LSP semantic adapter, Python fallback transport и recovery. Переход scanner в Rust не переносит reasoning модели
на локальный процессор. Методика [Performance/eval](PERFORMANCE_EVAL_RU.md) описывает
именно локальную часть; новых универсальных обещаний latency здесь нет.

## 4. Долгая задача и восстановление после compaction

![Checkpoint, handoff и свежесть](diagrams/context-recovery.svg)

[Редактировать в draw.io](diagrams/context-recovery.drawio) · [Все схемы](diagrams/README.md)

<details>
<summary>Mermaid source той же схемы</summary>

```mermaid
flowchart TD
    Active["Активная задача: contract, attempt и candidate"] --> Records["Receipts, results и ledger вне чата"]
    Active --> Decisions["Решения и следующий шаг"]
    Records --> Checkpoint["Checkpoint текущей attempt"]
    Decisions --> Checkpoint
    Checkpoint --> Handoff["Компактный handoff с references"]
    Handoff --> Compact["Native compaction или новая сессия Codex"]
    Compact --> Load["Восстановить цель, paths и ограничения"]
    Load --> Fresh{"Evidence и входы актуальны?"}
    Fresh -->|"Да, проверено"| Continue["Продолжить разрешённый шаг по current evidence"]
    Fresh -->|"Нет"| Attempt["Revalidation / repair: исходные attempts и deadline"]
    Fresh -->|"Не проверено"| Verify["resume или verify-evidence"]
    Verify --> Fresh
    Attempt --> Active
    Continue --> Active
```

</details>

Источниками истины служат текущие файлы и проверяемые records. Handoff помогает
вспомнить состояние, но не выдаёт старую заметку за новое доказательство.
Checkpoint проверяется на integrity/binding; notes прошлой attempt не
становятся инструкциями текущей автоматически.

`status --summary` дешёв в сравнении с полным audit evidence. По умолчанию
свежесть может быть unknown. `--verify-evidence`/`resume` выполняют соответствующую
перепроверку и могут вернуть stale. Новая attempt отделяет новую проверку от
старого исхода. Состояние `running` допускает отсутствие ещё не записанного
`result.json`; повреждённый готовый результат не считается `passed`.

Native compaction с порогом 500000 остаётся ответственностью Codex. Private
files уменьшают потерю контекста при сжатии, но не продлевают window модели и
не запускают задачу после закрытия клиента. Подробнее:
[эксплуатация](OPERATIONS.md), [контекст](CONTEXT_AND_NATIVE_RU.md).

## 5. Приёмка, locks и восстановление записей

Command runner связывает разрешённый executable, inputs, reports и attempt.
Перед приёмкой gate повторно проверяет состояние под state lock. Изменение
accepted receipt, входов или незавершённой внешней операции делает прежний
acceptance непригодным. JSON boolean не равен number; required test case обязан
быть действительно выполнен. Подробная схема находится в
[ACCEPTANCE.md](ACCEPTANCE.md).

Installer и rename используют разные локальные транзакции, но общий принцип:
сначала проверка всех доступных входов и возможности записи, затем staged
изменение, readback и при необходимости компенсация. В installer сохраняются
поддерживаемые security metadata; в rename plan связан с исходными bytes.
Recovery receipt фиксирует исход, который нельзя подтвердить как restored.

Файловый lock координирует процессы harness. Сторонний редактор может менять
файл независимо; общий атомарный commit всей файловой системы не обещается.
Неизвестный исход сохраняется как recovery state, а не скрывается коротким
ответом ради token budget. Диаграммы транзакций:
[установка](../SETUP_RU.md), [LSP rename](LSP_RU.md).

## 6. Что хранится и что передаётся

| Область | Содержимое | Жизненный цикл |
| --- | --- | --- |
| Переносимый пакет | Source, docs, policies, skills, schemas, native bundles релиза | Версионируется; package root должен оставаться доступным после установки |
| Codex home / skills home | Managed bootstrap, роли, skills, config changes и `harness-machine.json` pointer в Codex home | Обновление и откат через manifest |
| Private installation state | Machine overlay, backups, manifests и recovery journals | Отдельно для устройства/профиля; не публикуется в Git |
| Private run | Contract, snapshots, evidence, ledger, checkpoints | Отдельно для задачи; достаточно для разбора и продолжения |
| Process memory | RepoMap graph, routing hint, transport buffers | Завершается вместе с командой |
| Optional status MCP | Только разрешённые metadata выбранного private store | Запускается клиентом по явной настройке; нет автоматического background install |

Raw evidence сохраняется отдельно от краткой выдачи. Ограниченный scan на
secret patterns помогает отсеять известные формы, но состав packet перед
внешней передачей всё равно выбирается осознанно. Локальная работа не требует
внешних telemetry, memory services или копирования chat history.

## 7. Где смотреть остальные диаграммы

| Вопрос | Документ |
| --- | --- |
| Как устанавливаются и откатываются настройки | [SETUP_RU](../SETUP_RU.md) |
| Откуда новый чат получает инструкции | [AUTOLOAD_RU](AUTOLOAD_RU.md) |
| Как идут попытки и восстановление run | [OPERATIONS](OPERATIONS.md) |
| Когда evidence достаточно для acceptance | [ACCEPTANCE](ACCEPTANCE.md) |
| Как проверяется и применяется rename | [LSP_RU](LSP_RU.md) |

Все схемы доступны в [галерее draw.io](diagrams/README.md): `.drawio` содержит
редактируемые узлы/связи, `.svg` — эквивалентная самостоятельная векторная версия
для просмотра. Mermaid source
сохранён рядом с объяснением в Markdown. Галерея связывает эти представления с
модулями реализации. Private PNG для визуального review не входят в поставку.
Редактирование диаграмм не требует изменений model/provider или runtime harness.

## Optional граница инструментов и backends

```mermaid
flowchart LR
    C["Strict contract / DAG node"] --> P["execution selection + Tool Profile"]
    P --> R["Fresh dependency diagnostics / native renderer"]
    R --> Q{"Backend capabilities qualified?"}
    Q -->|"нет"| B["BLOCKED; model slot не занят"]
    Q -->|"да"| A["CodexAdapter / OpenCodeAdapter"]
    A --> S["Existing supervisor / owned process / exact session"]
    S --> E["JSONL events + existing YAML checkpoint / receipts"]
    E --> V["Frozen source / scope / independent checks / review / gate"]
```

[execution.py](../src/agent_harness/execution.py) связывает explicit selection с
существующим store. [tool_profiles.py](../src/agent_harness/tool_profiles.py)
валидирует закрытую policy и рендерит native client config;
[tooling_probe.py](../src/agent_harness/tooling_probe.py) проверяет короткое
диагностическое MCP соединение и всегда закрывает contained process tree.
Он не является proxy модельных вызовов и не доказывает effective client catalog.

Codex остаётся default. OpenCode — optional managed leaf transport, без
обязательного shared service/daemon. Backend не владеет scheduler, DAG,
worktrees, quotas, retries, integration и acceptance. OpenCode native subagents
не считаются managed workers; их полноценная оркестрация не реализована.
Native pre-turn verifier OpenCode проверяет location, catalog, permissions и
session persistence до model admission. Private native plugin проверяет
каталог каждого model request и отвергает незаявленные операции. Все runtime
catalogs и JSONL привязаны к completion receipts; утрата evidence закрывает gate.
При провале квалификации managed вызов блокируется. Смешанный DAG использует
прежние глобальные slots.

В guard сохраняются связанные `context`, `before_dispatch` и `after_response`
для каждого model request. Последняя квитанция требует прежние guard bytes и
живое соединение с обязательными MCP. Некоторые provider SDK заканчивают чтение
после terminal SSE и отменяют остаток HTTP body до сетевого EOF. Source после
1.7.0 проверяет EOF и подтверждённую consumer cancellation; потоковая ошибка
или отказ отмены не создают response-квитанцию. Отмена сама по себе не означает
успех: отдельно требуются полный native step/turn, exit 0 и подтверждённый
cleanup. Незавершённый поток остаётся `UNKNOWN_OUTCOME`.

Новые tooling receipts лежат в том же private run и доступны gate freshness
проверкам; отдельного state store нет. Graphify index — производный immutable
artifact исходного frozen snapshot с проверкой bytes на use boundary.
Ни MCP receipt, ни graph edge не заменяют независимое source/test evidence.
Подробности: [Tool Profiles](TOOL_PROFILES_RU.md),
[границы квалификации](TOOLING_ACCEPTANCE_RU.md).
