# Справочник CLI — 1.6.0

Этот справочник описывает текущие аргументы парсера и значения по умолчанию.
Практический полный сценарий — [эксплуатация](OPERATIONS.md#полный-локальный-пример),
критерии доказательств — [приёмка](ACCEPTANCE.md), подробности навигации —
[LSP](LSP_RU.md), контекст и Rust — [контекст и native](CONTEXT_AND_NATIVE_RU.md).

## Точки входа и формат

Из корня исходного пакета: `python tools/harness.py <команда>`. Из другого cwd
укажите полный путь к `tools/harness.py`. После установки Python-пакета доступны
`agent-harness <команда>` и `python -m agent_harness <команда>`. Это один dispatcher;
`work` запускает установленный Codex как отдельный процесс; остальные namespaces сохраняют прежнюю семантику. Native API клиента не эмулируется.

`--help` доступен у команд и вложенных подкоманд. `--version` печатает версию
пакета. Аргументы `--run-dir`, `--contract`, `--record` и output paths не имеют
неявного project-specific значения. В JSON-контракте `cwd` должен быть существующим
абсолютным каталогом; пути argv передаются отдельными строками, без shell.
Relative POSIX paths в `context`/`code` интерпретируются относительно `--root`.

Core/query команды обычно возвращают JSON в stdout. `work` поддерживает
`--format text|yaml|json`, по умолчанию `text`: status/report печатают строковую
сводку; отдельные metadata replies могут оставаться JSON. Обработанная
ошибка — JSON в stderr с exit 2. Ошибки самого `argparse`, включая неизвестный флаг,
также имеют exit 2, но используют обычный текст usage. Установочные commands используют собственный JSON output: в том числе blocked/error
печатаются в stdout. Build-команда native передаёт вывод build script и его exit
code. Полный managed JSON обычно форматирован;
`context read/map`, `code`, `status --summary` и `handoff` выводят компактный JSON.

| Exit | Значение |
| --- | --- |
| `0` | Запрос выполнен; для `check` — passed, для `gate` — accepted |
| `1` | `check` не passed; `gate` rejected; `resume` не разрешает нового owner; `status/handoff --verify-evidence` не подтверждает актуальную приёмку; `context verify` stale |
| `2` | Ошибка параметров/чтения/integrity/capability; `code apply` также использует 2 для failed_compensated/recovery_required |

У `plan next` пустая волна с причинами ожидания — штатный exit 0. У `doctor`,
`native status` и `code doctor` exit 0 означает успешную диагностику, а не наличие
всех optional executables или проверенную runtime capability. Смотрите поля ответа.
У установочного `verify` exit 1 означает неуспешную verification.

## Foreground work supervisor

`work` — отдельный namespace; legacy `init/check/gate` ниже сохраняется.

| Команда | Аргументы и действие |
| --- | --- |
| `work start` | `--contract F`; `--cwd` default current directory; optional `--run-dir`, `--state-dir`, `--codex`, `--plan`, `--native-disabled-proof`, `--max-planning-rounds`, `--format text\|yaml\|json` |
| `work status` | `--run-dir P`, optional `--summary`, `--verify-evidence`, `--format` |
| `work report` | `--run-dir P`, `--format`; сверяет актуальность evidence |
| `work watch` | `--run-dir P`; interval2s, timeout3600s, без model calls |
| `work resume` | `--run-dir P`; optional `--repair-journal`, `--new-attempt REASON`, `--codex`, `--plan F`, `--native-disabled-proof F`; original budgets сохраняются |
| `work stop` | `--run-dir P`, optional `--reason`; durable request, exact owned process cancellation |

В подкомандах `work`, где эти flags доступны, `--state-dir` задаёт global state
registry, а `--format` выбирает `text`, `yaml` или `json` с default `text`.
State directory по умолчанию берётся из установленного machine pointer, затем
platform state path. Новый run по умолчанию получает UUID под `state/runs`.
Путь global registry сохраняется в state; несовместимый override при resume
отклоняется. `work start/resume` имеют exit0 только при `ACCEPTED`, exit1 для
наблюдённого неуспеха, exit2 для parameter/integrity/capability errors. Status
различает recorded status и актуальную приёмку; stale acceptance не считается
accepted task в проверенном report.

Contract: [work schema](../schemas/work-contract.schema.json). Defaults:
max_attempts3 (initial edit включён), managed concurrency1, max_wall_seconds7200,
turn_timeout_seconds1800, mode native, ledger inform. Default `memory` — `false`
для contract v1 и `true` для contract v2; явное значение сохраняется. Global cap
managed sessions5; contract может понизить до1. Max tokens проверяется по native
reported usage на границах turns, не внутри скрытого model runtime.

`inputs` — до 128 объектов `{path, sha256?}`; `path` абсолютный ordinary file,
`sha256`, если указан, — lowercase SHA-256. Внешний/ignored input не включается
автоматически только из-за упоминания в argv. В managed mode явный acceptance
cwd должен находиться внутри source checkout; внутренние абсолютные paths
переносятся в staging. Встроенный исходный checkout path внутри строки Python
или shell-фрагмента отклоняется: сделайте проверку staging-safe.

| Work budget | Default | Допустимые значения |
| --- | --- | --- |
| `max_attempts` | 3 | 1–100; initial edit включён |
| `max_concurrent_managed_agents` | 1 | 1–5; native mode требует 1 |
| `max_wall_seconds` | 7200 s | 1–604800; исходный deadline не сбрасывается |
| `turn_timeout_seconds` | 1800 s | 1–86400; ограничен оставшимся deadline |
| `max_tokens` | Отсутствует | 1–100000000; проверяется по reported usage между turns |

После interruption неизвестный usage при заданном token cap блокирует
продолжение/приёмку; восстановление не делает расход нулевым.

```text
python tools/harness.py work ledger --help
python tools/harness.py work memory --help
python tools/harness.py work format-bench --input TABLE.yaml --real-codex --repeats 5 --output BENCH.yaml --evidence-dir /absolute/private/eval
python tools/harness.py work format-bench --input TABLE.yaml --render --evaluation BENCH.yaml --evidence-dir /absolute/private/eval
python tools/harness.py repomap related-tests --root /absolute/repo --target src/auth.py --revision HEAD
python tools/harness.py repomap trace-to-code --root /absolute/repo --trace-file TRACE.txt --revision HEAD
python tools/harness.py repomap change-impact --root /absolute/repo --changed src/auth.py --revision HEAD
python tools/harness.py repomap symbol-context --root /absolute/repo --path src/auth.py --symbol validate_token --revision HEAD
```

Format bench optional labels: `--model installed`, `--environment-fingerprint`,
`--output-type table`. Render использует только совпадающий measured positive
receipt, иначе JSON. Неполное выполнение/environment failure даёт
insufficient_data с diagnostics, а не утверждение о model quality. `repomap`
выдаёт bounded results с source hashes/rank/reason/next_page; heuristics не
гарантируют полноту. Полные recipes и ограничения:
[execution](EXECUTION_SUPERVISOR_RU.md), [memory](MEMORY_AND_CONTEXT_RU.md),
[dynamic](DYNAMIC_AGENTS_RU.md), [eval](PERFORMANCE_EVAL_RU.md).

`work ledger` поддерживает `show` и `rebuild`; `show` требует event ID и принимает
`--offset 0 --limit 4096` (default limit8192). `work memory` также поддерживает
`show` и `rebuild`; memory store и repository identity задаются явно.
Inform/govern и recall доступны через Python API и supervisor integration,
а не отдельные CLI actions.
Используйте возвращённый `next_offset` на UTF-8 boundary. RepoMap `--offset`
является индексом строки результата, не byte offset; `--max-bytes` по умолчанию
8192 учитывает serialized UTF-8 output. Неполный inventory указан отдельно.

## Совместимый Run API

| Команда | Обязательные и дополнительные аргументы | Результат и запись |
| --- | --- | --- |
| `init` | `--run-dir P --contract F` | Создаёт приватный run, frozen contract и исходный baseline; не freeze итогового candidate |
| `attempt` | `--run-dir P --contract F --reason TEXT` | Новый уникальный attempt ID в том же run/task, с новой гипотезой/требованием |
| `freeze` | `--run-dir P` | Фиксирует candidate и toolchains; stdout содержит candidate_digest, files, kind |
| `check` | `--run-dir P --check ID`; optional `--cancel-file F` | Запускает command check из контракта; сохраняет logs/reports/result, печатает краткую сводку |
| `evidence` | `--run-dir P --check ID --record F` | Записывает source/content/visual/runtime assessment с копиями артефактов |
| `result` | `--run-dir P --record F` | Owner передаёт ready_for_review с exact digests и evidence_ids; это не gate |
| `gate` | `--run-dir P` | Root проверяет inputs, scope, checks, receipts/dependencies и актуальный state; пишет verdict |
| `status` | `--run-dir P`; optional `--verify-evidence`, `--summary`, `--max-output-bytes N` | Read-only snapshot; summary сокращает ответ, verify проверяет сохранённую accepted evidence |
| `handoff` | `--run-dir P`; optional `--verify-evidence`, `--max-output-bytes N` | Read-only metadata/checkpoint view; без verify не сканирует workspace |
| `checkpoint` | `--run-dir P --note TEXT --next TEXT` | Версионированные решения/контекст, snapshot, state и next safe step |
| `resume` | `--run-dir P` | Наблюдает inputs/processes/evidence; записывает reconciliation state и safe_to_start_new_owner |
| `ledger` | `--run-dir P --record F` | Записывает наблюдённого native owner; не создаёт и не закрывает native thread |
| `capacity` | `--run-dir P` | Подсчитывает ledger по budget.native_cap/root_counts/source; не обращается к backend |
| `operation` | `--run-dir P --id ID --action TEXT --status S`; `--proof TEXT` default `""` | Outcome planned/unknown/confirmed/failed; confirmed/failed требуют непустой proof |
| `runs` | `--store P` | Перечисляет непосредственные `P/*/state.json`; не выполняет полную проверку каждого run |

`status --max-output-bytes` разрешён только вместе с `--summary`. Для summary и
handoff default 8192, диапазон 2048–65536 **UTF-8 bytes сериализованного JSON**,
с резервом финального CRLF. Полный `status`, `runs`, `plan` и обычные managed results
не имеют такого output budget; не считайте их неограниченно удобными для вставки
в историю. Длинные logs остаются в run store.

### Параметры command check в JSON

| Поле | Обязательность / default | Семантика |
| --- | --- | --- |
| `id`, `kind`, `required` | Обязательны; kind=`command`, required boolean | Уникальный ID и frozen обязательность |
| `argv` | Непустой массив строк | Прямой процесс; shell expansions не выполняются |
| `timeout` | Обязателен, `0 < N <= 86400`, секунды | Ограничивается оставшимся run deadline |
| `cwd` | Default `contract.cwd` | Существующий абсолютный каталог |
| `expected_exit` | `0` | Ожидаемый exit дочерней команды |
| `effect` | `local_test`; также `read_only` | Runner не предназначен для deploy/preparation/external action |
| `resources` | `[]` | Именованные cooperating locks, получаемые в сортированном порядке |
| `tool_versions` | `{}` | Именованные version-probe argv; имя `executable` зарезервировано |
| `assertions` | `[]` | regex + stream=`stdout`/`stderr`/`combined` (default), min_matches=1, forbidden=false |
| `reports` | `[]` | Локальный path и format=`json` или `junit`; отчёт должен быть произведён этим запуском |

JSON report `equals` сопоставляет dotted object paths с ожидаемыми JSON values.
Boolean отличается от number на любой глубине; `1` и `1.0` считаются численно
равными. JUnit defaults: `min_tests=1`, `max_skipped=0`, `required_cases=[]`.
GoogleTest `status=notrun`/`result=suppressed` и явные skipped/disabled cases не
закрывают required case; неизвестные execution status/result считаются ошибкой.
Обычный JUnit без execution attributes поддерживается.

Non-command checks используют `criteria` — непустой массив ID. JSON evidence
содержит kind, status=`satisfied|failed|not_run`, assessor, assessment, method,
criteria с объяснением каждого ID и artifact_paths. Методы: source_review для
source, agent_assessment/human_assessment для content,
agent_visual_review/human_visual_review для visual, observed_runtime для runtime.
Source требует также sources с URL/retrieved_at/claim/support/artifact; visual —
реально просмотренный image/PDF. Формат не удостоверяет личность автора.

Контракт: [JSON schema](../schemas/contract.schema.json), исполняемые проверки —
[`validate_contract`](../src/agent_harness/core.py). `budget.max_attempts=3` по
умолчанию; deadline отсутствует, пока не задан timezone-aware ISO timestamp.
Ordinary attempt не увеличивает лимит попыток и не продлевает/не удаляет deadline.

## Source context

| Команда | Аргументы и defaults | Ограничения и output |
| --- | --- | --- |
| `context pack` | `--root P --include FILE` (повторяемый) `--output F`; max-files=64, max-file-bytes=65536, max-bytes=262144 | Явный список, packet вне root, exclusive creation; source bytes, hashes, omitted/complete |
| `context verify` | `--root P --packet F` | Integrity/root binding + актуальные SHA включённых файлов; current/stale, не gate |
| `context read` | `--root P --file FILE`; start-line=1 **или** offset; lines=80, max-bytes=8192; optional expect-sha256 | Хэш всего UTF-8 файла и ограниченный excerpt; next_offset для продолжения |
| `context map` | `--root P`; query=`""`, focus-file/exclude повторяемые; остальные defaults ниже | Rust/Tree-sitter heuristic map, требует native binary; нет Python fallback |

Для pack флаги имеют вид `--max-files`, `--max-file-bytes`, `--max-bytes`.
Их диапазоны соответственно 1–256, 1–262144 и 1–1048576. **Здесь max-bytes —
суммарные исходные bytes включённого содержимого, не лимит JSON stdout.** Вход
serialized packet при verify ограничен 4 MiB. Слишком большой выбранный файл
может попасть в omitted; отсутствие `complete=true` нельзя выдавать за полный scope.

Для read: lines 1–2000, max-bytes 1024–65536 serialized UTF-8 bytes, вход до
16 MiB. Offset — **нулевой byte offset** на границе UTF-8; строки — с 1. Ответ
содержит полный sha256, start/end offsets, next_offset, truncated. `--expect-sha256`
принимает lowercase SHA-256 и предотвращает продолжение по изменившемуся файлу.

| `context map` флаг | Default | Допустимо |
| --- | --- | --- |
| `--max-bytes` | 8192 | 2048–65536 serialized UTF-8 bytes |
| `--max-files` | 4096 | 1–50000 |
| `--max-file-bytes` | 262144 | 1–1048576 |
| `--max-total-bytes` | 67108864 | 1–134217728 source bytes |
| `--timeout` | 10.0 | 0.1–30 секунд; wrapper допускает ещё 5 секунд для native call |
| `--query` | Пустая строка | До 4096 UTF-8 bytes |
| `--focus-file`, `--exclude` | Пустые списки | Повторяемые пути/исключения |

## LSP code

`AGENT_HARNESS_LSP_BACKEND=python|native` выбирает transport; default `python`.
Native доступен только на квалифицированном Linux x86_64/glibc с проверенным
bundle, без автоматического fallback после ошибки. Snapshot backend задаётся
отдельно. Для rollback уберите переменную или задайте `python` перед новой
Session; подробности — [LSP reference](LSP_RU.md).

| Команда | Обязательные аргументы | Особенности |
| --- | --- | --- |
| `code doctor` | Нет; `--root .` default | Optional file/server/language/max-bytes; серверы не запускаются |
| `code symbols` | `--root P --file FILE` | Document symbols, включая проверенные cross-file locations |
| `code definition` | То же + `--line N --column N` | Location/LocationLink |
| `code references` | То же + line/column | Declaration включено; optional `--exclude-declaration` |
| `code rename` | То же + line/column + `--new-name TEXT` | Preview; optional `--output F` сохраняет полный plan вне root |
| `code apply` | `--root P --plan F` | Проверка reviewed plan и компенсируемая файловая транзакция |

Общие query flags: `--server` (JSON argv), `--language`, `--settings` (JSON object),
`--timeout` (20 секунд, 0.1–120), `--max-bytes` (8192, 1024–262144 serialized UTF-8
bytes). Doctor/apply принимают только свои перечисленные flags; у apply нет
server/language/timeout/settings. Line/column — с 1, column считает Unicode code
points; провод LSP — UTF-16. new-name: 1–256 символов без control characters.

Навигация открывает захваченные целевые файлы и повторяет запрос до использования
диапазонов; максимум три дополнительных прохода. Точные limits, metadata/DACL,
статусы и восстановление — [LSP reference](LSP_RU.md).

## Native

| Команда / переменная | Default и поведение |
| --- | --- |
| `native status` | requested/selected/available/platform/path/build_info; adaptive policy при auto |
| `native build [--offline]` | Explicit local build; нужны полный source checkout, Cargo и C toolchain; offline требует подготовленного Cargo cache |
| `AGENT_HARNESS_BACKEND` | `auto`; также `python`, `native` |
| `AGENT_HARNESS_NATIVE` | Не задана; optional существующий **абсолютный** executable для developer override |

Auto при доступном проверенном binary выбирает Python для наблюдённого небольшого
дерева `<512` файлов; native — для большого/неизвестного дерева. Counting ограничен
20 ms и 64 каталогами; это routing hint, не inventory. `context map` вызывает
обязательную native repomap операцию независимо от Python routing snapshot.
Присутствующий повреждённый binary, неверный protocol, timeout или ошибка выполнения
не маскируются fallback. Details — [native boundary](CONTEXT_AND_NATIVE_RU.md#native-backend-и-маршрутизация).

## Planning helpers

У всех `plan` actions обязательны `--plan F --contract F`:

| Action | Дополнительно | Output |
| --- | --- | --- |
| `validate` | — | valid и IDs nodes; сверяет текущие contract/input digests |
| `brief` | `--node ID` | JSON с текстом точного owner brief |
| `next` | `--native-snapshot F` | Рекомендованная wave и причины blocked; не spawn/reservation |
| `repair-brief` | `--node ID --repair F` | Brief с failing check, log, expected и текущими bindings |

Helper читает файлы и может выполнять штатные read-only Git queries при snapshot;
не создаёт agents, worktrees, commits, новый gate и не изменяет run.
Формат/actual native inventory — [координация](ORCHESTRATION_RU.md).

## Установка и дополнительные интерфейсы

`doctor`, `install`, `verify`, `rollback`, `uninstall` маршрутизируются в installer.
Общие flags: package-root (default source package), codex-home (CODEX_HOME либо
`~/.codex`), skills-home (`~/.agents/skills`), state-dir, machine-overlay, apply,
native, cwd, manifest, max-child-threads. Значение и применимость флагов, выбор
private state, dry-run/apply/rollback — [SETUP](../SETUP_RU.md) и
[эксплуатация](OPERATIONS.md#диагностика-установки-и-откат). Наличие флага у общего
парсера не делает его действующим для любого installer action.

Optional MCP — отдельный read-only интерфейс metadata, см. [MCP_RU](MCP_RU.md).
Он не добавляет command checks, LSP writes или native spawn к CLI и не обязателен
для работы пакета. Native Codex controls принадлежат клиенту.

Исполняемые источники справочника: [cli.py](../src/agent_harness/cli.py),
[context.py](../src/agent_harness/context.py), [repo_map.py](../src/agent_harness/repo_map.py),
[lsp.py](../src/agent_harness/lsp.py), [native_cli.py](../src/agent_harness/native_cli.py),
[orchestration.py](../src/agent_harness/orchestration.py),
[installation.py](../src/agent_harness/installation.py),
[work_cli.py](../src/agent_harness/work_cli.py),
[work_contract.py](../src/agent_harness/work_contract.py),
[repomap_queries.py](../src/agent_harness/repomap_queries.py),
[format_bench.py](../src/agent_harness/format_bench.py). При расхождении установленного
`--help` с этой страницей сначала сверяйте версию/путь запускаемого пакета.

## Correction candidate: новые операции

`work migrate --run-dir RUN --expected-generation N` даёт dry-run. Apply требует
`--apply --expected-plan-digest SHA`; `--recover OPERATION_ID` явно завершает
pending migration. `work benchmark validate|schedule|report --manifest FILE
--assets-root ROOT` проверяет offline portfolio; report требует `--trials FILE`.
Это не solve runner и не разрешение на model расход.

Для отдельного managed v2 → v3 plan-quality upgrade используется
`work migrate --run-dir RUN --expected-generation N --target-version 3`.
Apply добавляет `--apply --expected-plan-digest SHA`. Необязательный
`--max-planning-rounds N` только уменьшает исходный предел; повторите его при
apply, если он был в dry-run. Pending v3 завершается явно:
`work migrate --run-dir RUN --expected-generation N --target-version 3 --recover OPERATION_ID`.
Recovery использует frozen policy и не принимает новый round limit. Команда
не запускает assessment или модель, сохраняет исходные counters/deadline и
оставляет план unassessed до следующего допустимого resume. Default без target3
сохраняет прежний v1 → v2 маршрут; автоматического upgrade при status/resume нет.
Форматы, backups и barrier описаны в [миграции состояния](STATE_FORMATS_AND_MIGRATION_RU.md).
Для нового managed contract2 run `work start --max-planning-rounds N` также
только уменьшает исходный planning limit; для native/legacy contracts override
отклоняется. Resume не принимает новый limit и сохраняет frozen policy.

`agent-harness-session goal-update --session ID --goal TEXT --expected-generation N [--operation-id ID]`
обновляет краткую active goal через CAS. `intake [--event-file FILE]` принимает
allowlisted metadata (default stdin); `status --root ROOT` выводит coverage.
`verify-result --session ID --run-dir CORE_RUN --expected-candidate SHA` связывает
существующую current core приёмку с изолированным candidate.
Memory `show ID --kind semantic|episode|short_term` разрешает typed namespace;
`migrate|gc --scope KIND [--apply]` сохраняет originals и unresolved refs.
Общие `--state`/`--max-bytes` у session передаются перед именем операции.

Точные contracts и ограничения: [Стабилизация](STABILIZATION_RU.md).

Deterministic facts: `work memory file-fact ID --root ROOT --path RELATIVE_FILE`
и `symbol-fact ID --root ROOT --path MODULE.py --symbol NAME`; обе команды требуют
`--store` и `--repository-identity`. `show --kind fact` читает их typed namespace.
Они подтверждают точные bytes/координаты, а не поведение произвольного приложения.

`agent-harness-session context-config` показывает private scan limits.
Изменение через `--checkpoint-scan '{"max_records":64}'` или `--memory-scan JSON`
требует `--expected-generation N`. Это не Codex model/permission/compaction settings.
Общий session transport budget принимается только в диапазоне 2048–65536 bytes.
Текущие goal bytes/digest/generation и constraints обязательны даже после source
change. Когда required envelope не помещается, `BUDGET_TOO_SMALL` требует большего
бюджета; обязательные поля не обрезаются. Interrupted generation publication
блокирует resume с `SESSION_RECOVERY_REQUIRED`; сохраняйте private marker и
immutable generation для repair. Полный протокол: [Memory и context](MEMORY_AND_CONTEXT_RU.md).

`install --adopt-hook-sha256 SHA` явно принимает существующую single-handler
SessionStart группу по SHA256 от installer canonical(group). По умолчанию чужой
harness handler остаётся конфликтом; mixed groups отклоняются и с digest.

Текущий SessionStart launcher возвращает bounded JSON с
`hookSpecificOutput.hookEventName = "SessionStart"` и строкой
`hookSpecificOutput.additionalContext`.
Wire budget включает весь serialized JSON и escaping. Если required context
не доставлен, hook сообщает degraded/not_delivered; успешный bootstrap не
заявляется. SessionStart с `session_id`/`cwd` без agent fields может установить
root scope, сохраняя observed agent identity как unknown. Config/JSON fixtures
и `verify --native` не подтверждают fresh-client lifecycle или trust конкретного
клиента: для этого нужна отдельная квалификация его версии и платформы.

## Verified managed contracts

`work start --contract <private-contract.yaml> --plan <private-plan.yaml> --cwd <source>` принимает согласованные contract/plan v2. В v2 обязательны mission/node_checks, у плана mission_digest/generation, а у узлов requirement_ids/checks.local/checks.integration. `work status --run-dir <run> --verify-evidence` показывает current requirements и проверяет raw node evidence. Без --plan managed v2 использует structured planner также при cap1. [Формат и ограничения](VERIFIED_TASK_FLOW_RU.md).

`work mission-update --run-dir <run> --contract <new.yaml> --reason <explicit-change>` сохраняет immutable successor mission без model call. `work resume --run-dir <run> --plan <new-plan.yaml>` продолжает новое поколение, сохраняя старые budgets/usage и проверяя новую mission binding.

`work benchmark task-flow-experiment prepare|verify|aggregate --help` подготавливает source-bound matched arms и агрегирует будущие independent reports без запуска моделей. [Входы, ограничения и примеры](TASK_FLOW_PREPARATION_RU.md).
