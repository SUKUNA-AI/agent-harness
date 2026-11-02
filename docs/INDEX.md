# Документация Native Agent Harness 1.5.1

Этот индекс разделяет актуальные инструкции, короткие процедуры для агента и
снимки прошлых проверок. Начальная точка — [README](../README.md).

## Установить и настроить

| Документ | Когда открыть |
| --- | --- |
| [SETUP_RU](../SETUP_RU.md) | Первая установка, обновление, перенос, dry-run, rollback и recovery; POSIX/PowerShell |
| [Конфигурация](CONFIGURATION_RU.md) | Источники настроек, модели/effort, child cap, 500000 и optional output budget |
| [Автоподключение](AUTOLOAD_RU.md) | AGENTS/override, skills, новый чат и проверка реального клиента |
| [Native bundles](../bin/README.md) | Готовый бинарник, сборка, version/hash mismatch и поддерживаемые платформы |

## Понять и применять

Новый `work` namespace описан в следующих действующих руководствах:

| Документ | Содержание |
| --- | --- |
| [Execution Supervisor](EXECUTION_SUPERVISOR_RU.md) | Start/status/watch/resume/stop/report, exact sessions, bounded repair |
| [Memory и context](MEMORY_AND_CONTEXT_RU.md) | Ledger inform/govern, provenance, memory, reducer, четыре RepoMap query |
| [Dynamic agents](DYNAMIC_AGENTS_RU.md) | Native/managed mode, global slots, DAG, staging integration |
| [State и migration](STATE_FORMATS_AND_MIGRATION_RU.md) | Strict YAML, схемы, journal и сохранение старых JSON |
| [Performance/eval](PERFORMANCE_EVAL_RU.md) | Реальные usage, A/B, TOON eligibility и insufficient_data |
| [Источники](SOURCES_AND_EVALS.md) | Перенесённые механизмы и границы научных claims |

| Документ | Содержание |
| --- | --- |
| [Архитектура](ARCHITECTURE_RU.md) | Слои, границы Python/Rust/Codex, поток контекста и восстановление |
| [Галерея draw.io](diagrams/README.md) | Редактируемые XML, реально экспортированные SVG, связи с Mermaid source и кодом |
| [CLI reference](CLI_REFERENCE_RU.md) | Все маршруты, аргументы, defaults, единицы лимитов и exit codes |
| [Эксплуатация](OPERATIONS.md) | Полный локальный пример managed run, status, attempt, checkpoint и resume |
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

## Релизы, проверки и архив исследования

Текущая линия: [1.5.1](RELEASE_NOTES_1.5.1.md); сохранённая база:
[1.5.0](RELEASE_NOTES_1.5.0.md) и [1.4.2](RELEASE_1_4_2.md). Полная история:
[CHANGELOG](../CHANGELOG.md).

| Снимок | Что зафиксировано |
| --- | --- |
| [Исправления A1–A11 в 1.5.1](AUDIT_FIXES_1_5.md) | Candidate binding, процессные слоты, budgets, recovery, Ledger/memory и Linux rename; границы совместимости |
| [Baseline перед 1.5.0](BASELINE_1_5_0.md) | Фактически проверенная исходная main 1.4.2; не финальная приёмка 1.5.0 |
| [Аудит 1.4.1](REVIEW_1_4_1.md) | Исправления R01–R14, metadata, гонки, LSP recovery и native build |
| [Проверка 1.4.0](REVIEW_1_4_0.md) | Перенос scanner/snapshot в Rust, RepoMap, LSP и измерения |
| [Проверка 1.3.0](REVIEW_1_3_0.md) | Bounded output, handoff, reader races и CLI overhead |
| [Проверка 1.2.0](REVIEW_1_2_0.md) | F01–F11, contracts, evidence, rollback и зависимости |
| [Проверка 1.1.1](REVIEW_2026-11-01.md) | Первое исправление общего пакета |
| [Проверка 1.1.0](RELEASE_REVIEW.md) | Историческая исходная поставка |
| [Ecosystem screening](ECOSYSTEM_RU.md) | Датированный обзор закреплённых upstream, не текущий install list |
| [Memory/TokenEconomizer](MEMORY_TOKENS_RU.md) | Исследованные, но не включённые дополнительные системы |

Числа тестов, benchmark и ограничения старого отчёта относятся к его версии.
Состояние нового релиза проверяется по его commit, CI, отдельному verification
report и встроенному `release-manifest.json` с hashes payload файлов.
