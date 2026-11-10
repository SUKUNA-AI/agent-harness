# Документация Native Agent Harness 1.6.1

Этот индекс содержит актуальные руководства и короткие процедуры для агента. Начальная точка — [README](../README.md).

Контракты исполнения, включённые в 1.6.0, и границы приёмки:
[Стабилизация исполнения](STABILIZATION_RU.md).

## Установить и настроить

| Документ | Когда открыть |
| --- | --- |
| [SETUP_RU](../SETUP_RU.md) | Первая установка, обновление, перенос, dry-run, rollback и recovery; POSIX/PowerShell |
| [Конфигурация](CONFIGURATION_RU.md) | Источники настроек, модели/effort, child cap, 500000 и optional output budget |
| [Автоподключение](AUTOLOAD_RU.md) | AGENTS/override, skills, новый чат и проверка реального клиента |
| [Native-сессия](NATIVE_SESSION_RU.md) | Установленный SessionStart hook, короткий bootstrap, checkpoint и проверка свежести |
| [Native bundles](../bin/README.md) | Готовый бинарник, сборка, version/hash mismatch и поддерживаемые платформы |

## Понять и применять

Команды `work` и проверяемая очередь описаны в следующих действующих руководствах:

| Документ | Содержание |
| --- | --- |
| [Execution Supervisor](EXECUTION_SUPERVISOR_RU.md) | Start/status/watch/resume/stop/report, exact sessions, bounded repair |
| [Memory и context](MEMORY_AND_CONTEXT_RU.md) | Ledger inform/govern, provenance, memory, reducer, четыре RepoMap query |
| [Dynamic agents](DYNAMIC_AGENTS_RU.md) | Native/managed mode, global slots, DAG, staging integration |
| [Verified task flow](VERIFIED_TASK_FLOW_RU.md) | Mission/plan v2, plan-quality gate, node acceptance, coverage и fresh review |
| [State и migration](STATE_FORMATS_AND_MIGRATION_RU.md) | Strict YAML, схемы, journal и сохранение старых JSON |
| [Performance/eval](PERFORMANCE_EVAL_RU.md) | Реальные usage, A/B, TOON eligibility и insufficient_data |
| [Источники](SOURCES_AND_EVALS.md) | Перенесённые механизмы и границы научных claims |

| Документ | Содержание |
| --- | --- |
| [Архитектура](ARCHITECTURE_RU.md) | Слои, границы Python/Rust/Codex, поток контекста и восстановление |
| [Галерея draw.io](diagrams/README.md) | Редактируемые XML, эквивалентные SVG, связи с Mermaid source и кодом |
| [CLI reference](CLI_REFERENCE_RU.md) | Все маршруты, аргументы, defaults, единицы лимитов и exit codes |
| [Эксплуатация](OPERATIONS.md) | Полный локальный пример совместимого ручного Run API: status, attempt, checkpoint и resume |
| [Приёмка](ACCEPTANCE.md) | Evidence, receipts, freshness, gate и независимый evaluator |
| [Координация](ORCHESTRATION_RU.md) | Делегация, DAG, slots, ownership, review и repair |
| [Контекст и Rust](CONTEXT_AND_NATIVE_RU.md) | RepoMap, packets, excerpts, budgets, long-running tasks и производительность |
| [LSP](LSP_RU.md) | Symbols/definition/references, Unicode positions, rename preview/apply и recovery |
| [Безопасность](SECURITY_RU.md) | Недоверенные данные, permissions, narrow cache и ограничения изоляции |
| [Разработка](DEVELOPMENT_RU.md) | Границы модулей, tests, docs/diagram workflow и подготовка релиза |
| [Карта документации](DOCUMENTATION_MAP_RU.md) | Текущие guides, архивы и устанавливаемые instructions; связь с кодом |

## Интеграции и инструкции модели

| Документ | Назначение |
| --- | --- |
| [Интеграции](../integrations/README.md) | Выбор доступного tool по задаче и optional modules |
| [MCP](MCP_RU.md) | Локальный stdio adapter для ограниченных metadata статусов |
| [Plugin](PLUGIN_RU.md) | Независимый Harness Assistant из двух skills |
| [Выбранные механизмы](SELECTED_PATTERNS_RU.md) | Что реализовано из изученных подходов и где проходит граница |
| [Общая политика](../policies/global.md) | Компактные правила, загружаемые через bootstrap |
| [Workflow контекста](../workflows/context.md) | Практический выбор чтения, packets, handoff и wiki |
| [Browser acceptance](../workflows/browser.md) | Проверка пользовательского web-сценария |
| [Review](../workflows/review.md) | Findings, ограничения и сводная волна исправлений |
| [Offline experiments](../workflows/experiments.md) | Сравнение prompts/skills на фиксированных критериях |

Подробные справочники загружаются по задаче. Наличие большого README не означает
его автоматическую вставку в каждый prompt. `roles/`, `skills/`, `workflows/`
и `templates/` содержат короткие исходные assets для установки и работы агента.

## Текущая версия и история

Действующие руководства описывают 1.6.1 в `main`.
[Состав выпуска 1.6.1](RELEASE_NOTES_1.6.1.md) и его manifest связывают архив
с исходниками. Предыдущий [1.6.0](RELEASE_NOTES_1.6.0.md) закреплён своим тегом;
новые изменения `main` не меняют его содержимое. История остаётся в
[CHANGELOG](../CHANGELOG.md), Git и [GitHub Releases](https://github.com/SUKUNA-AI/agent-harness/releases).
Устаревшие отдельные release/review Markdown удалены из рабочего дерева.

## Verified task flow

- [Проверяемая очередь задач](VERIFIED_TASK_FLOW_RU.md) — contract/plan v2, node gates и текущая приёмка.
- [Протокол matched сравнения](TASK_FLOW_EXPERIMENT_RU.md) — подготовка последующих model runs.
- [Подготовка source-bound task-flow эксперимента](TASK_FLOW_PREPARATION_RU.md) — supplied bank, frozen policies и unknown-aware reports.
