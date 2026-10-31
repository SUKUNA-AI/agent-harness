# Native Agent Harness 1.1.0

Общая система работы в локальном Codex: постановка задачи, краткий план,
полезная делегация, изолированные исполнители, проверка результата и восстановление
контекста. Пользователь формулирует результат обычным сообщением; служебные
контракты, брифы и команды готовит основной агент.

Для содержательной задачи по умолчанию работают два native владельца; третий
подключается при независимой части и доступной ёмкости. Короткие задачи идут
напрямую. Неделимость, ограничения пользователя или нехватка native slots дают
последовательный путь. Все потомки и idle/open refs учитываются вместе.
Модель/provider и безопасность наследуются. Усилие координатора сохраняется;
узкая механика — medium, содержательная реализация и ревью — high.

```mermaid
flowchart LR
    U[Задача пользователя] --> R[Постановка и краткий план]
    R -->|короткая задача| D[Прямое выполнение и проверка]
    R -->|независимые части| P[Брифы и зависимости]
    P --> A[Владелец 1]
    P --> B[Владелец 2]
    A --> I[Интеграция]
    B --> I
    I --> V[Независимая проверка]
    V -->|исправление прежнему владельцу| P
    V -->|актуальные доказательства| G[Итоговая приёмка]
```

## Состав

| Компонент | Назначение |
|---|---|
| `policies/global.md`, `adapters/codex/bootstrap.md` | Общая политика и короткий автоматически подключаемый prompt |
| `workflows/`, `roles/` | Процедуры исследования, реализации, review, документов, эксплуатации и recovery |
| `src/agent_harness/`, `schemas/` | Contracts, snapshots, checks/evidence/gates, ledger, checkpoints и read-only DAG helpers |
| `skills/` | Создание skills, экономия контекста, integrations и автоматическая orchestration |
| `plugins/harness-assistant/` | Отдельный переносимый plugin с двумя самостоятельными skills |
| `integrations/` | Маршрутизация tools, public docs MCP и пример локального read-only connector |
| `profiles/` | Справочные пользовательские правила и настройки без credentials |
| `provenance/` | Происхождение исходника и pinned реестр изученных upstream |

Python не вызывает LLM и не создаёт native agents. Он детерминированно проверяет
план/состояние и готовит briefs; spawn/follow-up выполняют штатные tools Codex.
Файлы и worktrees обеспечивают координацию, а не отдельный OS sandbox.

## Установка

Нужен Python 3.11+; ядро использует stdlib. Работайте из полного source checkout:
wheel без policy/workflow assets не является самостоятельной поставкой.
Ниже `<package>`, `<codex-home>`, `<skills-home>` и `<private-state>` заменяет
агент по текущей среде. Private state хранится вне репозитория.

```text
python <package>/tools/harness.py doctor --codex-home <codex-home> --state-dir <private-state>
python <package>/tools/harness.py install --codex-home <codex-home> --skills-home <skills-home> --state-dir <private-state>
python <package>/tools/harness.py install --codex-home <codex-home> --skills-home <skills-home> --state-dir <private-state> --apply
python <package>/tools/harness.py verify --codex-home <codex-home> --skills-home <skills-home> --state-dir <private-state> --native --cwd <empty-project>
```

Первый install — dry-run с адресным diff; apply создаёт backups/manifest.
Сохраняются неизвестные TOML keys, comments, пользовательский текст и чужие роли.
Установщик управляет `agents.enabled`, cap и отсутствующим `interrupt_message`;
model/provider/reasoning/security не меняет. Portable default cap — 4 children;
существующий более строгий cap сохраняется. Явный `--max-child-threads N` адресно
разрешает иной cap. Число логических направлений, target active owners и backend
slots — разные значения; фактическую ёмкость наблюдают в текущем клиенте.

Непустой `AGENTS.override.md` имеет приоритет над `AGENTS.md`. Ставятся managed
bootstrap, machine pointer, три native роли и skills. Пути обновляются через
installer/machine overlay. Одноимённые чужие skills или ручные изменения managed
файлов вызывают конфликт без перезаписи. Если новый пакет удалил прежний owned
skill/resource, обновление останавливается до явной миграции: старый активный
skill не оставляется незаметно. Новая установка не гарантирует hot reload старого
чата; локальные файлы не настраивают hosted ChatGPT Work.

```text
python <package>/tools/harness.py rollback --state-dir <private-state>
python <package>/tools/harness.py rollback --state-dir <private-state> --apply
python <package>/tools/harness.py uninstall --state-dir <private-state>
python <package>/tools/harness.py uninstall --state-dir <private-state> --apply
```

Rollback/uninstall сначала проверяют всю транзакцию и backups. Чужие edits,
репозитории, runs, процессы и файлы, созданные самим Codex, не удаляются.

## Повседневное использование

Примеры обычных запросов:

- «Реализуй изменение и проверь конечный сценарий».
- «Проверь этот PR, исходники не меняй».
- «Исследуй варианты по актуальным первичным источникам».
- «Подготовь документ полностью и проверь оформление».
- «Покажи статус задачи» или «Продолжи по сохранённому checkpoint».

Root сам определяет intent/scope, план, общие inputs, owners и достаточную
приёмку. Review/plan/research не разрешают product edits. Внешние действия
выполняются при действующем основании задачи; зелёные tests его не создают.
Подробности: [orchestration](docs/ORCHESTRATION_RU.md),
[операции](docs/OPERATIONS.md), [критерии приёмки](docs/ACCEPTANCE.md).

## Plugin и бесплатный локальный MCP

[Plugin](docs/PLUGIN_RU.md) можно установить отдельно через включённый marketplace.
Он содержит самостоятельные coordination/status skills и не подменяет global
bootstrap. Lifecycle hooks и автоматическое подключение аккаунтов отсутствуют.

[Read-only MCP](docs/MCP_RU.md) предоставляет ограниченные metadata статусов из
явно выбранного private store. Нет raw prompts, goals, файлов, логов, network
или выполнения команд. Optional dependency — официальный `mcp==2.3.0`.
Он не включается в live profile автоматически. [Другие integrations](integrations/README.md)
подключаются по конкретной задаче после проверки уже доступных tools.

## Готовые open source решения

[Обзор](docs/ECOSYSTEM_RU.md) содержит source screening нескольких десятков
репозиториев, pins, лицензии и решения по применимости. ECC/Superpowers полезны
выборочными skills; Spec Kit/OpenSpec/GSD — проектными процессами; Worktrunk,
Serena и browser tools — специализированными возможностями. Их source support
не считается нашей runtime-проверкой. LangGraph/Langfuse не обязательны для
нынешнего native control plane; rationale и ограничения приведены в обзоре.
Отдельно разобраны [Claude-Mem и TokenEconomizer](docs/MEMORY_TOKENS_RU.md):
native Codex support подтверждён исходниками, подготовлен conservative профиль
оптимизатора без auto-prune; глобальная память и фоновые sidecalls не включены.

## Проверки и доказательства

```text
python tools/release_check.py
python tools/run_tests.py
python -m pip install "mcp==2.3.0"
python -I -B tools/check_mcp.py
```

CI запускает эти проверки на Windows/Ubuntu и Python 3.11/3.13. Наличие YAML не
равно успешному запуску — сверяйте Actions конкретного commit. Реально выполненные
проверки и ограничения: [RELEASE_REVIEW](docs/RELEASE_REVIEW.md).
Tests не заменяют native/visual/runtime acceptance. Unknown/not_run/skipped
не passed; ускорение и экономия токенов не объявляются без измерения.

Исходный snapshot 1.0.1 сохранён в provenance; проектные адаптеры, auth,
machine state, приватные runs и история исходного Git в поставку не входят.
