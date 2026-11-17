# Проверка Tool Profiles и execution backends

[Профили](TOOL_PROFILES_RU.md) · [Codex](CODEX_MCP_RU.md) ·
[OpenCode](OPENCODE_RU.md) · [Verified flow](VERIFIED_TASK_FLOW_RU.md)

Tool receipt подтверждает наблюдение об инструменте. Приёмку кода по-прежнему
определяют scoped diff, frozen candidate, независимые checks, review и final
gate существующего supervisor. Никакая запись `MCP success` их не заменяет.

## Граница квалификации

Детерминированные tests запускают настоящие CLI/MCP subprocesses с fixtures,
Git worktrees и штатным journal/gate. Они проверяют реализацию harness без
токенов. Это **не live квалификация Serena, Graphify или model client**.

`tools catalog --backend codex` отдельно запускает краткоживущий native
app-server без model turn. Его результат относится к его ephemeral thread,
изолированному CODEX_HOME и указанной конфигурации. Даже `READY` с живым
`runtimeStatus=connected` сохраняет `qualified_for_exec: false`: этот процесс
не является последующим managed `codex exec`.

Для профиля с MCP execution adapter должен иметь реализованную и проверенную
capability `effective_tool_catalog`. Legacy CodexAdapter не объявляет её;
отдельный StrictCodexAdapter получает её только после успешной core-issued
native qualification. Наличие CLI bridge не подменяет эту проверку.
Для нового tooling DAG также требуется квалифицированная изоляция final
reviewer **до запуска workers**, включая случай отсутствующего
`review_execution`. Нативный каталог MCP не доказывает отсутствие shell,
plugins, Code Mode или внешних write-capable операций reviewer.

Поэтому строгий managed MCP/reviewer workflow без свежего proof завершается
`BLOCKED_NOT_QUALIFIED`. Это намеренный отказ. Флага, YAML boolean или
операторского self-report для обхода квалификации нет. Старый контракт без
новых полей сохраняет прежнюю модель исполнения и прежние гарантии.

OpenCode 2.2.5 имеет native core verifier: supervisor квалифицирует конкретный
worktree/роль перед dispatch, а private plugin проверяет реальные context и
HTTP catalogs каждого model request. Его JSONL artifacts и normalized execution
log закрепляются в completion receipt. Сохранённый diagnostic proof не является
операторским разрешением; drift и утрата runtime evidence закрывают gate.

## Матрица возможностей

| Возможность | Реализация | Live граница |
| --- | --- | --- |
| Codex baseline без новых полей | SUPPORTED | Сохраняется существующий flow |
| Strict профили, render/dry-run, dependency probes | SUPPORTED | Отсутствующая dependency видна отдельно |
| Native Codex MCP catalog/deny RPC | QUALIFIED для изолированного probe | Не квалификация последующего exec |
| Strict Codex owned app-server API | EXPERIMENTAL; QUALIFIED для Linux/Codex 0.167.0 и local scripted provider | Source read, allow/deny, same-session restart/history; fresh per-project qualification обязательна, paid workload не квалифицирован |
| MCP profile в managed execution | Отдельные qualified backend boundaries | OpenCode guard; Strict Codex owned app-server transport требует собственного native proof, legacy exec его не наследует |
| Строгий reviewer с MCP | QUALIFIED для OpenCode 2.2.5 с fixture MCP | Реальный клиент и malicious provider negatives; реализация конкретного MCP требует аудита |
| Serena на каждый worktree | QUALIFIED для launcher 2.1.0 и OpenCode 2.2.5 | Две concurrent native stdio sessions, затем два native OpenCode workers с реальными symbol calls; local scripted provider, scope/cleanup |
| Optional Context7 public read | QUALIFIED для 4.6.0 | Реальные public resolve/query; authenticated/private documents не квалифицированы |
| Graph index provenance/invalidation | QUALIFIED для local-code-only Graphify 0.9.111 | Реальный AST index; source/package/parser/interpreter drift отрицательные проверки |
| ast-grep migration helper | QUALIFIED для 0.51.3 | Native before/rewrite/after и независимая Python `-B` regression; scope guard |
| OpenCode V2 managed leaf transport | QUALIFIED для 2.2.5 | Native pre-turn/client policy и runtime catalog enforcement |
| OpenCode exact session resume | QUALIFIED для rehearsal и наблюдённого cloud сценария | Реальный restart и два Hydrogen/Flex turns с тем же ID; не произвольный workload |
| OpenCode native multi-agent mode | NOT_IMPLEMENTED | Не эквивалент managed workers |
| Configured browser runner/profile | QUALIFIED для native MCP/Chromium scenario | Official MCP 0.0.85, finite actions, independent regression/artifacts; model admission отдельно |
| Documents/Docling | EXCLUDED по выбору оператора | Не устанавливался; обычная разработка и baseline от него не зависят |
| HTTP MCP diagnostic transport | NOT_IMPLEMENTED | HTTP renderer не означает живую готовность |

`SUPPORTED` описывает интерфейс реализации; `QUALIFIED` относится только к
указанной проверенной границе. `SKIP`, `NOT_AVAILABLE`, `BLOCKED`, unsupported
version и `UNKNOWN_OUTCOME` не являются успешной приёмкой.

### Наблюдения live-квалификации 2026-11-17

Наблюдения этого раздела выполнены на Linux. Они не квалифицируют новые
optional tool boundaries на Windows/macOS; прежняя baseline поддержка пакета
и её release evidence имеют собственные границы.

Новый Strict Codex API остаётся experimental. На Linux/Codex 0.167.0
реальный client прошёл source read и same-session restart с прежней историей
через local scripted provider; установленная конфигурация прошла coding + Serena
и read-only reviewer qualification. Квалификация требуется заново для каждого
project/run; её отсутствие даёт `BLOCKED_NOT_QUALIFIED`. Платный Codex workload
этими проверками не квалифицируется.

OpenCode 2.2.5 подтвердил native effective catalog и exact resume для
coding-context7 (Serena + Context7) и настроенного browser-testing (8 MCP tools).
Native MCP IDs сохраняют дефисы: `context7_query-docs`,
`playwright-managed_browser_snapshot`. Эти проверки не заменяют source/test gate.

Serena, Context7, Graphify и ast-grep проверены установленными native packages,
а не renamed fixtures. Полные receipts остаются в private run evidence;
версии и границы приведены выше. Новая машина, executable bytes, package/parser
или interpreter drift требуют fresh checks. Тесты
`test_local_tool_integrations.py` различают deterministic regression и opt-in
native package tests (`HARNESS_LIVE_LOCAL_TOOLS=1`). Наличие исходника теста не
означает, что native scenario выполнен на другом host.

Отдельный OpenCode+Serena proof запустил двух настоящих OpenCode workers
в разных Git worktrees с разными session IDs и реальными `serena_find_symbol`
calls. Native guard/каталог и разные реализации symbol наблюдались в ходе
исполнения; source/cleanup checks прошли. Provider был локальным scripted
endpoint, поэтому ни платный inference, ни полный supervisor final gate не
заявляются. Новый final source/helper snapshot требует новый Graphify index:
previous qualification не разрешает reuse historical receipt после code drift.

Browser suite выполнил семь настоящих official MCP/Chromium сценариев:
isolated lifecycle/cleanup, finite catalog и запреты code/upload/path/origin/
redirect, воспроизведение regression и проверка исправления. Дополнительные
wiring tests проверяют profile schema, lazy render, CLI и dynamic dependencies.
Чистый browser qualification не доказывает final source gate или permissions
выбранного model client. Базовый browser profile без absolute policy остаётся
`CONFIG_REQUIRED`; он не получает localhost/origin/executable по догадке.

Cloud OpenCode доказал два native Hydrogen/Flex turns с точным session
continuation. Наличие opaque encrypted reasoning metadata и source-backed
сохранение известного формата подтверждены отдельно; outgoing-wire byte proof
не выполнялся. Global default model не менялась. Подробности —
[OpenCode](OPENCODE_RU.md#проверенный-hydrogenflex-сценарий-и-opaque-reasoning-metadata).

### SSE regression и actual model + MCP после исправления

Исходный реальный Hydrogen/Flex + Serena + Context7 turn выполнил tool calls,
но не имел обязательного `after_response` и остался `UNKNOWN_OUTCOME`.
Исправление guard в source revision `277519e3494e5878670f57e36aba357ac990547e`
покрывает network EOF и подтверждённый consumer cancel terminal SSE.
Опубликованный ZIP 1.7.0 этого исправления не содержит.

Новый отдельный Linux smoke наблюдал один реальный OpenRouter
`openrouter/google/gemini-4-hydrogen` turn, только route
`google-ai-studio/flex`, `allow_fallbacks:false`, с двумя завершёнными MCP calls:
`serena_find_symbol` и `context7_resolve-library-id`. Проверены post-response
audit, неизменность source и остановка owned process tree.
Статус — `OBSERVED_REAL_OPENROUTER_MCP`, acceptance — `not_performed`.
Это не полный supervisor workload benchmark или final gate и не квалификация
платного Codex, других моделей, других routes либо optional tools на Windows.
Private receipts, session IDs, auth и host paths не входят в Git.

Отдельный targeted regression запуск для SSE/native OpenCode/tool profiles
дал **77 tests: 76 PASS, 1 SKIP**, exit 0. Он включает семь новых lifecycle
tests настоящего guard с Node Web Streams; реальные native negative/permissions/
resume сценарии используют isolated local scripted provider без платных calls.
Это уже выполненные scoped evidence, а не результат запуска команд ниже
читателем. Старый full baseline **1370 PASS, 75 SKIP** относится к другому
исходному snapshot и не является полной проверкой source revision с SSE fix.
Текущая граница поставки — [Unreleased notes](RELEASE_NOTES_NEXT.md).

## Семь обязательных сценариев

| ID | Автоматизированное доказательство | Что остаётся live |
| --- | --- | --- |
| A1 | `test_tooling_acceptance`: fixture managed CLI workers/worktrees/MCP, scoped integration, review/gate. Отдельно native Serena 2.1.0: две stdio sessions и разные результаты symbol поиска | Native launcher test не заменяет полный managed model/gate run с реальной Serena |
| A2 | `test_tool_profiles`: frozen source и invalidation. Отдельно Graphify 0.9.111: настоящий local AST extraction и source/package/parser/interpreter drift | Другие версии/семантический LLM graph не квалифицированы |
| A3 | Native OpenCode 2.2.5: forced shell/edit/subagent/MCP write/execute, external read, forged plugin ID, удаление guard; нет записи/RPC. Native Codex: diagnostic allowlist/deny RPC и отдельный exec read/denied namespace proof | Строгий Codex RPC/resume boundary требует собственной qualification; конкретные read-only MCP требуют аудита |
| A4 | Worker process interruption, supervisor restart и explicit retry сохраняют binding; drift блокирует запуск. Отдельно OpenCode: два настоящих Hydrogen/Flex turns, прежний session ID | Full workload gate и wire bytes opaque reasoning payload не проверены этим сценарием |
| A5 | Startup/handshake/missing tool/timeouts/disconnect; model slot отсутствует при preflight failure; native OpenCode теряет fake MCP после provider dispatch и не сообщает completed, даже без MCP tool calls | Реальные отказы установленных сторонних сервисов |
| A6 | Старый контракт без execution/profile; Codex flow проходит без создания OpenCode, без новых MCP; прежние schemas/checkpoints и scheduler покрыты regression suite | Windows/native qualification выполняется отдельно |
| A7 | Успешные MCP calls и worker integration не спасают проваленный independent check; положительный A1 проходит freeze/check/review/gate; удаление tool receipt делает сохранённый ACCEPTED устаревшим | Live model workload отдельно от harness fixtures |

## Запуск проверок

Основной набор использует stdlib unittest и vendored Strict YAML:

```bash
python tools/release_check.py
python tools/run_tests.py
```

Точечные группы:

```bash
python -m unittest discover -s tests -p test_tool_profiles.py -v
python -m unittest discover -s tests -p test_tooling_contracts_cli.py -v
python -m unittest discover -s tests -p test_codex_execution.py -v
python -m unittest discover -s tests -p test_opencode_execution.py -v
python -m unittest discover -s tests -p test_opencode_response_lifecycle.py -v
python -m unittest discover -s tests -p test_tooling_acceptance.py -v
python -m unittest discover -s tests -p test_codex_tool_catalog.py -v
```

Отдельный opt-in native catalog test, без inference/token:

```bash
HARNESS_LIVE_CODEX_CATALOG=1 python -m unittest discover -s tests -p test_codex_tool_catalog.py -v
HARNESS_LIVE_OPENCODE_QUALIFICATION=1 python -m unittest discover -s tests -p test_opencode_qualification.py -v
```

PowerShell: сначала `$env:HARNESS_LIVE_CODEX_CATALOG='1'`, затем та же Python
команда. Для OpenCode live smoke используйте `tools smoke --backend opencode`:
успешный help или standalone startup не выдаётся за exact-resume qualification.

Opt-in OpenCode suite использует установленный CLI, изолированные HOME/XDG,
реальный native permission/plugin/MCP runtime и локальный scripted model
endpoint. Он не требует платного ключа. На Linux с 2.2.5 проверены девять native
live сценариев, включая detached Git worktree, отказ native worktree remove
и точечный read-only доступ к evidence с отказом чтения соседнего файла и записи.
Это отдельно от доступа к OpenRouter.
Без opt-in, CLI или local listen тесты имеют явную причину skip/block, которая
не считается qualification другой машины. `tools qualify --backend opencode`
сохраняет bounded native preflight без inference в указанном private каталоге.

Для длительного набора допустимы штатные непересекающиеся shards:
`python tools/run_tests.py --shard 1 --shards 4` и остальные три номера.
Все четыре exit codes обязательны; один зелёный shard не равен всему suite.
Во время тестов candidate Python/schema files должны оставаться неизменными.

## Контракт и два workers

Новые optional поля верхнего контракта или отдельного task plan:

```yaml
tooling_schema_version: 1
execution:
  backend: codex
  tool_profile:
    id: project-search
    version: 1
    path: /absolute/private/search-profile.yaml
  required_capabilities: [json, resume, effective_tool_catalog]
```

Task selection наследует явно указанные верхние значения; `review_execution`
отдельно выбирает backend/profile финального reviewer. Например:

```yaml
review_execution:
  backend: codex
  tool_profile: {id: reviewer-readonly, version: 1}
```

`execution.backend: opencode` разрешён для managed leaf. Native OpenCode
отклоняется semantic validation. Отсутствующие поля не дописываются в старый
нормализованный контракт, поэтому его сохранённый digest не меняется молча.

Два независимых task узла получают разные write scopes, а контракт задаёт
`budgets.max_concurrent_managed_agents: 2`. Используйте существующие команды:

```bash
python tools/harness.py work start --help
python tools/harness.py work start --contract /absolute/private/task.yaml --plan /absolute/private/plan.yaml --cwd /absolute/repo --state-dir /absolute/private/state
```

Передайте точные обязательные аргументы, показанные установленным `--help`.
Supervisor сам создаёт worktrees. Не добавляйте `codex --worktree`. Смешанный
план не создаёт отдельной квоты: все backends используют общие 12 slots и до
3 reviewers. Fixture mixed DAG и два OpenCode workers проверяются через тот
же scheduler, integration и final gate.

Внешний клиент не получает полномочий создавать дополнительные sessions.
Конкурентный Codex по-прежнему требует действующее suppression evidence;
OpenCode без квалифицированных leaf permissions не допускается к запуску.

## Provenance и resume

Новый `tool-execution.schema.json` описывает immutable YAML в `tooling/`
существующего run store. `graph-index.schema.json` описывает graph receipt.
Invocation/completion используют существующее поле `capability_receipt_ref`
и `input_refs`; второй execution store не появляется.

Исторические `work-contract.schema.json`, `agent-plan.schema.json`,
`agent-state.schema.json`, `work-state.schema.json` и `work-state.v3.schema.json`
сохраняют исходные bytes. Новые поля описаны отдельными
`work-contract.tooling.v1.schema.json` / `agent-plan.tooling.v1.schema.json`.
`tooling_schema_version: 1` возникает только при явном выборе execution или
review_execution; обычный normalized contract/plan и его digest не меняются.

Новые tooling agents используют `agent-state.v3.schema.json`, старые agent1/2
читаются без добавления полей. Новый runtime reader `work-state.v4.schema.json`
принимает исторические state1/2/3 и отдельный state4. Только opt-in контракт
либо versioned план активирует явный checkpoint upgrade в том же journal:
`schema_version: 4`, `tooling_schema_version: 1`, `base_schema_version: 2|3`.
Deadline, attempts, session/usage и прежние checkpoints сохраняются; base3
сохраняет обязательный plan-quality gate. Historical migration2/3 продолжает
использовать старый reader и отказывается от state4 без изменения файлов.
Не выполняйте downgrade или редактирование state вручную.

Binding содержит backend executable/version/hash и launcher files digest,
profile/version/digest, native-config digest, fingerprints user/project config
bytes, catalog digest, dependency binary/server-info fingerprints, canonical
worktree path и directory/ownership identity, source snapshot, base revision,
graph receipt digest при наличии. Значения credentials туда не копируются.
Session ID, process identity, JSONL logs и checkpoint остаются в штатных
agent state и execution receipts.

До exact resume повторно проверяются profile/config/catalog/MCP versions,
launcher files, worktree и source checkpoint. Отсутствующий checkpoint нового
tooling agent блокирует продолжение. При required graph проверяется актуальное
соответствие source bytes; dirty source не скрывается за прежним HEAD SHA.

Пример operator workflow:

```bash
python tools/harness.py work stop --run-dir /absolute/private/state/work-RUN --reason 'controlled interrupt'
python tools/harness.py work status --run-dir /absolute/private/state/work-RUN --verify-evidence
python tools/harness.py work resume --run-dir /absolute/private/state/work-RUN
```

Не всякое прерывание разрешает новую попытку: persisted cancellation,
неопределённый исход, живой/неподтверждённый процесс и original deadline
сохраняют штатные ограничения recovery. `--new-attempt` с конкретной новой
гипотезой не увеличивает quotas, wall deadline или attempt budget. Для нового
tooling leaf оно может разрешить продолжение **того же** подтверждённо
остановленного cancelled/timed-out session; отсутствие session ID не создаёт
новую сессию под видом exact resume.

## Диагностика и rollback

Сначала `tools show`, затем `tools dry-run`, `tools check`, `tools catalog`.
В отчёте проверяйте readiness и qualification отдельно. `SERVER_MISSING`,
`STARTUP_FAILED`, `HANDSHAKE_FAILED`, `MISSING_TOOL`, policy denial, timeout,
disconnect и unknown cleanup имеют разные diagnostic reasons.

Сохраняйте original journal, immutable receipts и логи с exit codes. После
изменения source/index/profile/binary прежняя память и cached catalog не
подтверждают новую приёмку. `work status --verify-evidence` читает сохранённые
доказательства; он не запускает новую модель или повтор внешнего действия.

Глобальные настройки не изменяются. `tools apply --apply --output ...`
создаёт только новый private per-run файл, отказывается от traversal,
symlinks, существующего файла и user/project config roots. Для rollback
прекратите использовать этот файл или профиль. Старый контракт без новых
полей продолжает исходный Codex baseline. Releases, credentials и global
installation этот workflow не меняет.
