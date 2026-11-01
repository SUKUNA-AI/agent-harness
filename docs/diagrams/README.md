# Редактируемые диаграммы 1.5.0

[Индекс документации](../INDEX.md) · [Архитектура](../ARCHITECTURE_RU.md)

Здесь находятся 13 схем действующих руководств. `.drawio` — незапакованный
XML с редактируемыми shapes/connectors; `.svg` — реальный экспорт draw.io
Desktop **32.6.1**. Mermaid source сохранён в соответствующем Markdown под
раскрываемым блоком. PNG использован для visual review и хранится только в
private evidence. Чтение SVG не требует draw.io, JavaScript или сети.

| Просмотреть SVG | Редактируемый source | Объяснение и Mermaid source | Реализация |
| --- | --- | --- | --- |
| [Задача, исполнители и приёмка](task-delivery.svg) | [.drawio](task-delivery.drawio) | [README.md](../../README.md), Mermaid #1 | [orchestration.py](../../src/agent_harness/orchestration.py), [runner.py](../../src/agent_harness/runner.py) |
| [Установка, проверка и откат](installation-recovery.svg) | [.drawio](installation-recovery.drawio) | [SETUP_RU.md](../../SETUP_RU.md), Mermaid #1 | [installation.py](../../src/agent_harness/installation.py) |
| [Слои исполнения 1.5.0](runtime-architecture.svg) | [.drawio](runtime-architecture.drawio) | [docs/ARCHITECTURE_RU.md](../ARCHITECTURE_RU.md), Mermaid #1 | [supervisor.py](../../src/agent_harness/supervisor.py), [codex_execution.py](../../src/agent_harness/codex_execution.py), [core.py](../../src/agent_harness/core.py) |
| [Выбор источника контекста](context-routing.svg) | [.drawio](context-routing.drawio) | [docs/ARCHITECTURE_RU.md](../ARCHITECTURE_RU.md), Mermaid #2 | [context.py](../../src/agent_harness/context.py), [repo_map.py](../../src/agent_harness/repo_map.py), [lsp.py](../../src/agent_harness/lsp.py) |
| [Выбор snapshot backend](snapshot-backends.svg) | [.drawio](snapshot-backends.drawio) | [docs/ARCHITECTURE_RU.md](../ARCHITECTURE_RU.md), Mermaid #3 | [native.py](../../src/agent_harness/native.py), [native_cli.py](../../src/agent_harness/native_cli.py), [core.py](../../src/agent_harness/core.py) |
| [Checkpoint, handoff и свежесть](context-recovery.svg) | [.drawio](context-recovery.drawio) | [docs/ARCHITECTURE_RU.md](../ARCHITECTURE_RU.md), Mermaid #4 | [core.py](../../src/agent_harness/core.py), [presentation.py](../../src/agent_harness/presentation.py) |
| [Инструкции нового процесса Codex](instruction-discovery.svg) | [.drawio](instruction-discovery.drawio) | [docs/AUTOLOAD_RU.md](../AUTOLOAD_RU.md), Mermaid #1 | [installation.py](../../src/agent_harness/installation.py), [diagnostics.py](../../src/agent_harness/diagnostics.py) |
| [Совместимый ручной Run API](legacy-run.svg) | [.drawio](legacy-run.drawio) | [docs/OPERATIONS.md](../OPERATIONS.md), Mermaid #1 | [core.py](../../src/agent_harness/core.py), [runner.py](../../src/agent_harness/runner.py) |
| [Gate и повторная проверка evidence](acceptance-gate.svg) | [.drawio](acceptance-gate.drawio) | [docs/ACCEPTANCE.md](../ACCEPTANCE.md), Mermaid #1 | [runner.py](../../src/agent_harness/runner.py) |
| [Supervisor: edit, verify, repair](supervisor-repair.svg) | [.drawio](supervisor-repair.drawio) | [docs/EXECUTION_SUPERVISOR_RU.md](../EXECUTION_SUPERVISOR_RU.md), Mermaid #1 | [supervisor.py](../../src/agent_harness/supervisor.py), [failure_policy.py](../../src/agent_harness/failure_policy.py) |
| [Память, provenance и адресный retrieval](memory-provenance.svg) | [.drawio](memory-provenance.drawio) | [docs/MEMORY_AND_CONTEXT_RU.md](../MEMORY_AND_CONTEXT_RU.md), Mermaid #1 | [execution_ledger.py](../../src/agent_harness/execution_ledger.py), [memory.py](../../src/agent_harness/memory.py), [context_reducer.py](../../src/agent_harness/context_reducer.py) |
| [DAG, worktrees и единая приёмка](managed-worktrees.svg) | [.drawio](managed-worktrees.drawio) | [docs/DYNAMIC_AGENTS_RU.md](../DYNAMIC_AGENTS_RU.md), Mermaid #1 | [scheduling.py](../../src/agent_harness/scheduling.py), [managed_execution.py](../../src/agent_harness/managed_execution.py), [agent_lifecycle.py](../../src/agent_harness/agent_lifecycle.py) |
| [LSP rename: preview, apply и recovery](lsp-rename.svg) | [.drawio](lsp-rename.drawio) | [docs/LSP_RU.md](../LSP_RU.md), Mermaid #1 | [lsp.py](../../src/agent_harness/lsp.py), [rename.py](../../src/agent_harness/rename.py) |

## Как обновить схему

1. Сверьте фактические состояния, переходы и ограничения по указанным модулям.
2. Откройте `.drawio` в [draw.io Desktop](https://github.com/jgraph/drawio-desktop).
   Используйте локальную копию; runtime приложения не включён в harness.
3. Обновите узлы/связи и соответствующий Mermaid block в руководстве.
4. Экспортируйте SVG через draw.io и просмотрите его. Для проверки можно
   отдельно экспортировать PNG вне репозитория. Зафиксируйте version, argv,
   source/export SHA-256 и просмотренный артефакт в private review evidence.
5. Проверьте Markdown links и отсутствие private paths, remote images/fonts,
   scripts и непереносимых `file:` links в XML/SVG. При visual defect исправьте
   source и повторите export/review, а не только картинку.

Из package root, если CLI draw.io Desktop доступен как `drawio`:

```sh
drawio --export --format svg --theme light --embed-svg-fonts false --border 24 --timeout 30 --output docs/diagrams/supervisor-repair.svg docs/diagrams/supervisor-repair.drawio
drawio --export --format png --theme light --border 24 --timeout 30 --output /absolute/private/supervisor-repair.png docs/diagrams/supervisor-repair.drawio
```

На Windows используйте полный путь к portable `draw.io.exe` через PowerShell
оператор `&`; замените output PNG на свой private path. Для автоматизации
можно задать отдельный `--user-data-dir=<private-directory>` и `--disable-update`,
не изменяя основной профиль приложения. SVG использует обычный локальный
Arial/sans-serif, без web-font загрузок. Отдельные shell/OS способы скрытого
запуска не являются параметрами модели или настройками harness.

## Границы схем

Это обзор логики, а не полная спецификация FSM или permission sandbox.
Supervisor diagram показывает основной repair/recovery путь; cancellation
и timeout могут прервать несколько фаз. DAG diagram не обещает ускорение.
Память и Ledger не повышают authority наблюдений до пользовательской policy.
Совместимый ручной Run API отделён от foreground `work` namespace. LSP sequence
сохраняет capture/requery, отдельный reviewed apply и допустимую компенсацию.

Цвет дублируется подписями: решение обозначено ромбом и текстом; стрелки
содержат условия. Редактируемые объекты не являются скриншотом Mermaid.
