# Карта документации 1.6.0

[Индекс](INDEX.md) · [Диаграммы draw.io](diagrams/README.md)

Эта карта отделяет действующие инструкции от датированных результатов.
Документ описывает интерфейс; фактический outcome подтверждают test/evidence
receipts конкретного commit, среды и candidate. Дата актуализации: 2026-11-09.

## Действующие руководства

| Документ | Основной источник поведения |
| --- | --- |
| [README](../README.md) | Общие границы runtime, installer, CLI и release builder |
| [SETUP](../SETUP_RU.md) | `installation.py`, `diagnostics.py`, templates установленных assets |
| [CHANGELOG](../CHANGELOG.md), [Release notes](RELEASE_NOTES_1.6.0.md) | Состав 1.6.0, source tag, assets и ограничения проверок |
| [Архитектура](ARCHITECTURE_RU.md) | Границы Codex, supervisor, core runner, Rust и LSP |
| [CLI](CLI_REFERENCE_RU.md) | `cli.py`, `work_cli.py`, nested dispatchers и актуальный `--help` |
| [Supervisor](EXECUTION_SUPERVISOR_RU.md) | `supervisor.py`, `work_contract.py`, `failure_policy.py` |
| [Dynamic agents](DYNAMIC_AGENTS_RU.md) | `scheduling.py`, `managed_execution.py`, `agent_lifecycle.py` |
| [Memory/context](MEMORY_AND_CONTEXT_RU.md) | `execution_ledger.py`, `memory.py`, `context_reducer.py`, `repomap_queries.py` |
| [State/migration](STATE_FORMATS_AND_MIGRATION_RU.md) | `strict_yaml.py`, `supervisor_store.py`, authoritative `schemas/` |
| [Performance/eval](PERFORMANCE_EVAL_RU.md) | `telemetry.py`, `format_bench.py`; measured receipts отдельно от claims |
| [Sources/evals](SOURCES_AND_EVALS.md) | Первичные источники идей, лицензии dependencies, границы воспроизведения |
| [Конфигурация](CONFIGURATION_RU.md) | Адресные TOML changes установщика, default/explicit/role precedence |
| [Autoload](AUTOLOAD_RU.md) | Managed bootstrap и native configuration discovery новым процессом |
| [Native session](NATIVE_SESSION_RU.md) | `native_session.py`, `native_observability.py`, SessionStart и receipts |
| [Verified task flow](VERIFIED_TASK_FLOW_RU.md) | `plan_quality.py`, `verified_flow.py`, `completion_eligibility.py` |
| [Task preparation](TASK_FLOW_PREPARATION_RU.md) | `task_flow_experiment.py`, matched arms, frozen bank/assets и prepare/verify/aggregate без model calls |
| [Stabilization](STABILIZATION_RU.md) | Durable receipts, dependency generations, migrations и границы 1.6.0 |
| [Operations](OPERATIONS.md) | Совместимые `core.Run`/`runner` JSON contracts, воспроизводимый локальный пример |
| [Acceptance](ACCEPTANCE.md) | Freeze/check/evidence/gate, свежесть и независимая приёмка |
| [Orchestration](ORCHESTRATION_RU.md) | Native plan helpers в `orchestration.py`; separate managed mode — в Dynamic agents |
| [Context/native](CONTEXT_AND_NATIVE_RU.md) | `context.py`, `repo_map.py`, `native.py` и Rust helper |
| [LSP](LSP_RU.md) | `lsp.py`, `rename.py`: capture/requery, preview/apply и recovery |
| [MCP](MCP_RU.md) | `mcp_server.py`: optional read-only legacy run metadata; work status через CLI |
| [Plugin](PLUGIN_RU.md) | Independent Harness Assistant 1.3.1, две процедуры без runtime hooks |
| [Security](SECURITY_RU.md) | Permissions, cooperating locks, typed IO и недоверенные данные |
| [Development](DEVELOPMENT_RU.md) | Модули, проверки, author/reviewer workflow и release requirements |
| [Selected patterns](SELECTED_PATTERNS_RU.md) | Реализованные механизмы и pinned provenance |
| [Draw.io gallery](diagrams/README.md) | Все 16 editable XML/SVG пар, Mermaid sources и ссылки на реализацию |

Пути модулей относятся к `src/agent_harness/`. Optional reuse дополняет эту
карту: `input_closure.py`, `check_cache.py` и `python_runtime.py` доказывают
конечный input/runtime closure; arbitrary tests и ordinary Python runtimes
выполняются заново. Final required runner/gate не принимает старый cache PASS.

## Версия и история

Руководства описывают текущий `main`. [Релиз 1.6.0](RELEASE_NOTES_1.6.0.md)
фиксирует опубликованный архив. Старые отдельные release/review snapshots
удалены; история изменений сохранена в [CHANGELOG](../CHANGELOG.md), Git
и [GitHub Releases](https://github.com/SUKUNA-AI/agent-harness/releases).

## Короткие устанавливаемые инструкции и сопутствующие assets

Подробные guides не загружаются в каждый prompt. Следующие файлы содержат
короткие правила, routing или шаблоны; они используют текущие руководства:

- [AGENTS](../AGENTS.md), [global policy](../policies/global.md),
  [Codex skill](../adapters/codex/SKILL.md) и [bootstrap](../adapters/codex/bootstrap.md).
- [Workflows](../workflows/direct.md), [delivery](../workflows/delivery.md),
  [planning](../workflows/planning.md), [context](../workflows/context.md),
  [recovery](../workflows/recovery.md), [operations](../workflows/operations.md),
  [document](../workflows/document.md), [review](../workflows/review.md),
  [research](../workflows/research.md), [experiments](../workflows/experiments.md),
  [performance](../workflows/performance.md), [browser](../workflows/browser.md),
  [learning](../workflows/learning.md).
- [Doc author](../skills/harness-doc-author/SKILL.md),
  [doc review](../skills/harness-doc-review/SKILL.md),
  [orchestrate](../skills/harness-orchestrate/SKILL.md),
  [integrations](../skills/harness-integrations/SKILL.md),
  [skill creator](../skills/harness-skill-creator/SKILL.md),
  [token economy](../skills/harness-token-economy/SKILL.md).
- [Owner](../roles/lane-owner.md), [helper](../roles/helper.md),
  [reviewer](../roles/reviewer.md); [handoff](../templates/handoff.md),
  [wiki](../templates/project-wiki.md), [review packet](../templates/review-packet.md).
- [Integrations](../integrations/README.md), [domain adapter](../adapters/domains/README.md),
  [hosted Work boundary](../adapters/chatgpt-work/README.md),
  [native bundle](../bin/README.md), [personal example](../profiles/AGENTS.personal.example.md).
- Plugin skills [coordinate](../plugins/harness-assistant/skills/harness-coordinate/SKILL.md)
  и [run status](../plugins/harness-assistant/skills/harness-run-status/SKILL.md).

Vendored licenses, parser manifest, native third-party notices и Cargo pins
сохраняют собственные форматы и атрибуцию. Изменение описаний или draw.io assets
не разрешает заменять dependency license либо включать private runtime logs.
