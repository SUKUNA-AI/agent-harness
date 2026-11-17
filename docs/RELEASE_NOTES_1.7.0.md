# Agent Harness 1.7.0

Agent Harness 1.7.0 добавляет optional Tool Profiles, проверяемые native MCP
границы Codex/OpenCode и реальные локальные инструменты для поиска кода,
архитектурного анализа и browser acceptance. **Codex и baseline остаются
defaults**. Прежний workflow не требует установки новых внешних MCP/CLI.

Основа выпуска — проверенный candidate
`d58aac54cc675f01bf08213689ef051341f38bdd`, tree
`e53776c1b17a35d1c60152749c35c4dd31197d7f`. Финальная revision поставки и
состав assets закрепляются release manifest.

## Core и проверяемое исполнение

- Strict YAML profile задаёт backend, роли, MCP servers, CLI dependencies,
  allow/deny/required tools, worktree/cache binding и timeout policy.
- Logical profile identity отделена от runtime execution profile, где уже
  раскрыты конкретные paths и dynamic dependencies. Profile/config/toolchain
  drift блокирует продолжение вместо silent fallback.
- Native catalogs и execution artifacts закрепляются SHA-256 в существующих
  invocation/completion receipts, checkpoint и journal. Отдельного execution
  store, database и дополнительного LLM API client не добавлено.
- Supervisor сохраняет DAG, scopes, один writer на worktree, общую квоту до
  12 managed sessions, включая до 3 reviewers, independent checks, review,
  requirement coverage и final gate. Review policy повторно проверяется после
  ожидания slot и перед dispatch.
- Успешный tool call или completed model turn не заменяет приёмку candidate.
  Потеря или подмена закреплённого runtime artifact делает evidence устаревшими.

Подробности: [архитектура](ARCHITECTURE_RU.md),
[supervisor](EXECUTION_SUPERVISOR_RU.md),
[матрица приёмки](TOOLING_ACCEPTANCE_RU.md).

## Tool Profiles и реальные инструменты

Доступны `baseline`, `coding`, `architecture`, `reviewer-readonly`,
`browser-testing`, `documents`, `coding-context7` и `architecture-context7`.
Новые dependencies включаются выбранным сценарием; core installer их
автоматически не скачивает.

| Возможность | Что реализовано и проверено |
| --- | --- |
| Serena 2.1.0 | Native stdio launcher, tools interface, пять read operations, private HOME/XDG/project/cache, physical worktree binding; два параллельных worktrees возвращают свои symbol results |
| Context7 4.6.0 | Optional anonymous public documentation reads, private Node environment и implementation binding; включается явными `*-context7` профилями |
| Graphify 0.9.111 | Настоящий local-code-only AST graph из frozen source, index/source/settings/toolchain receipts, отказ при source/package/parser/interpreter drift |
| ast-grep 0.51.3 | Before scan → scoped rewrite → after scan → independent regression; проверка source после regression, без автоматических downloads |
| Official Playwright MCP 0.0.85 | Finite browser facade, isolated Chromium/context, explicit origin policy, bounded artifacts и cleanup; отдельный finite Node browser job с independent regression |

Native Python launchers используют `-I` до загрузки helper. Prepared argv,
implementation bytes и private policy проверяются; похожее имя executable или
тот же version label сами по себе не дают допуска.

Browser profile требует явных `allowed_origins`, `mcp_package`,
`browser_executable`, `browser_cache`. До настройки результат —
`CONFIG_REQUIRED`. Поддержаны exact numeric loopback HTTP origins; arbitrary
JavaScript, upload и внешние origins не входят в finite tool boundary.

[Tool Profiles](TOOL_PROFILES_RU.md) ·
[операторский runbook](TOOLING_OPERATIONS_RU.md) ·
[CLI reference](CLI_REFERENCE_RU.md)

## Codex и managed OpenCode

**Legacy Codex exec/resume сохраняется.** Дополнительный strict managed
transport выбирается явно и использует owned stdio app-server: конкретный
thread/start или thread/resume, turn/start с output schema и native MCP census
до/после turn. Это не постоянно устанавливаемый daemon.

Strict transport остаётся **EXPERIMENTAL**. На Linux с Codex 0.167.0 проверены
source read, native allow/deny, process cleanup и same-session/history restart
через local scripted provider; отдельно прошли coding + Serena и read-only
reviewer qualification в установленной конфигурации. **Платный Codex workload
не квалифицирован.** Каждый project/run требует свежего core-issued proof;
диагностический catalog или operator boolean не заменяет execution admission.

**OpenCode V2 2.2.5** поддержан как optional managed leaf. Native permissions
и private runtime guard проверяют effective catalog и фактические calls.
Подтверждены exact session restart, Serena/Context7 и browser native tool chains,
а также отрицательные сценарии shell/edit/subagent/MCP write/execute,
external reads, чужих plugins и утраты guard/artifacts.

Native MCP IDs сохраняют дефисы: `context7_query-docs` и
`playwright-managed_browser_snapshot`. Shell/execute/skill/subagent не входят
в qualified leaf grants; tests/compilers запускает supervisor. У OpenCode нет
native output-schema flag: результат отдельно валидирует core.

Отдельные реальные OpenCode/OpenRouter двухходовые проверки с прежним session
ID прошли для Gemini Hydrogen/Flex, Claude Opus 5.5 и DeepSeek V4.1 Flash.
Это scoped model evidence, не гарантия любой модели, версии, provider route
или полного пользовательского workload. Для opaque reasoning metadata
outgoing-wire byte proof не заявляется.

[Codex MCP](CODEX_MCP_RU.md) · [OpenCode](OPENCODE_RU.md)

## Совместимость и восстановление

Контракт без новых tooling/execution fields сохраняет прежний normalized
digest и baseline behavior. Исторические contract/plan/state и receipt schemas
сохранены. Tooling использует отдельные закрытые contract/plan variants,
work-state v4 с base version 2/3 и completion-only catalog variants:
`opencode_catalog` и отдельный `codex_catalog`. Старые receipts и journal
digests не переписываются для включения нового backend.

Exact resume сверяет session ID, process tree, worktree ownership,
profile/config/dependency/index provenance и remaining budgets. `--last`,
новая сессия с пересказом или ручное удаление lock не заменяют reconciliation.
Attempts, token cap и исходный deadline не сбрасываются.

Порог `compile_packet` 24000 UTF-8 bytes переключает inline payload на
immutable required parts/manifest, а не обрезает обязательные данные.
В strict tooling внешние parts вне worktree без exact read grants дают
`BLOCKED_NOT_QUALIFIED` **до dispatch**; автоматических private-part grants
worker не получает. Direct OpenCode schema+prompt cap остаётся отдельной
границей интерфейса. Tool schema `maxLength` не является model context window.

[State и migration](STATE_FORMATS_AND_MIGRATION_RU.md) ·
[recovery](../workflows/recovery.md)

## Проверки и границы доказательства

На frozen **d58aac54…** полный набор выполнен в 12 непересекающихся shards:
**1445 tests: 1370 passed, 75 skipped, 0 failed**. Candidate HEAD/tree и чистота
исходников после выполнения сохранились. Skipped относятся к отсутствующим
platform/runtime/opt-in capabilities и не считаются PASS этих сценариев.

Это результат исходного candidate, **не новый full run release candidate
1.7.0**. Для переноса regression evidence применяется отдельный независимый
source-equivalence review: execution code, schemas и tests должны совпадать,
кроме пяти канонических version literals. Release checks, native builds и
packaging проверяются заново; результат и точная revision указаны в
`VERIFICATION.json` и manifest поставляемого архива.

Для d58aac54… также проверены сборка portable ZIP с Linux/Windows native
bundles, extracted isolated install/idempotence/rollback smoke и реальная
локальная установка с structural/native verification. Эти проверки относятся
к исходному candidate; финальная упаковка 1.7.0 проверяется отдельно.

Deterministic integration, native clients с local scripted provider, реальные
внешние tools и оплачиваемые model calls имеют разные границы. Ни один scoped
PASS не объявляет принятым произвольный production проект или unsupported host.

## Установка или обновление из ZIP

После публикации используйте **полный** `agent-harness-1.7.0.zip` и
соответствующий `SHA256SUMS` из выбранного выпуска. GitHub «Source code ZIP»
и Git clone не заменяют архив с готовыми native bundles.

1. Сравните SHA-256: `sha256sum agent-harness-1.7.0.zip` на POSIX либо
   `Get-FileHash .\agent-harness-1.7.0.zip -Algorithm SHA256` в PowerShell.
2. Распакуйте пакет в постоянный каталог. Сохраните прежний package и private
   state; сверяйте source revision и состав по manifest архива.
3. Из package root выполните `doctor` и `install` без `--apply`, используя
   прежние Codex home, skills home и state-dir. Просмотрите targeted diff.
4. Выполните `install --apply`, затем `verify`; повторный `install --apply`
   должен вернуть `unchanged`.
5. Откройте новый процесс Codex и выполните применимую `verify --native`.
   Structural verification и наличие hook файла не заменяют native check.

Полные команды POSIX/PowerShell, конфликты hooks и rollback —
[SETUP_RU](../SETUP_RU.md). Публикация сама не обновляет установленный профиль.
Model/provider/effort, auth и глобальная безопасность не меняются для
прохождения qualification. Уже открытый чат не получает обещания hot reload
или фонового продолжения после закрытия клиента.

## Известные ограничения

- OpenCode-native multi-agent mode — **NOT_IMPLEMENTED**; доступны managed
  leaves под существующим supervisor.
- Strict Codex transport — **EXPERIMENTAL**, paid Codex workload не проверен.
- Новые optional live tool boundaries на Windows/macOS **не квалифицированы**.
  Наличие Windows native bundle не доказывает их runtime support.
- Browser boundary ограничена finite actions и numeric loopback HTTP origins;
  это не универсальная sandbox враждебного браузера или host.
- Fingerprints имеют явные bounds и не покрывают полностью host system shared
  libraries; source/tool provenance не является универсальной OS attestation.
- Docling **исключён и не устанавливался**. `documents` profile не является
  зависимостью обычной инженерной работы; его отдельная qualification не
  объявляется выполненной.
- Missing required dependency, drift, disconnect, неизвестный process cleanup,
  skipped/not_run и потеря receipt не превращаются в PASS.

Подробная навигация и editable draw.io/SVG схемы:
[индекс документации](INDEX.md), [галерея схем](diagrams/README.md).
