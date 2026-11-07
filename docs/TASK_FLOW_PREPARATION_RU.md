# Подготовка сравнения длительных workflow

`agent_harness.task_flow_experiment` подготавливает воспроизводимый локальный
эксперимент поверх существующих benchmark assets, Git capture, independent grade
и usage schemas. Модуль не запускает модель, evaluator, runner или supervisor,
не скачивает корпуса и не регистрирует production adapter.

Публичные API: `prepare(spec_path, *, assets_root, source_roots, output_dir)`,
`verify(preparation_dir, *, expected_digest=None)` и
`aggregate(preparation_dir, reports, *, reports_root)`. Subject-bound producers:
`write_mission_grade(path, *, preparation_dir, trial_id, execution_evidence, reports_root)`
и `write_trial_report(path, *, preparation_dir, evaluator_receipt, reports_root, report)`.
CLI integration point —
`dispatch(argv)`, для forwarding из основного CLI без дополнительного процесса.
В основном CLI доступен `work benchmark task-flow-experiment prepare|verify|aggregate`; standalone CLI работает через `python -m agent_harness.task_flow_experiment`.

## Supplied task bank

Пользователь предоставляет strict YAML/JSON specification с record type
`task_flow_experiment`; полная закрытая схема —
[`task-flow-experiment.schema.json`](../schemas/task-flow-experiment.schema.json).
Она также описывает original mission, shared decomposition, hidden evaluator,
configured effort, future policy observation, preparation и future trial/grade.
Неизвестные поля, duplicate keys, nonfinite numbers и выход за parser bounds
отклоняются. SHA256 refs проверяют точные исходные bytes до нормализации.

Spec содержит `experiment_id`, `bank_kind`, `bank_provenance`, `comparison`,
`tasks`, `policy`, пять `arms` и список `seeds`. `bank_kind` явно выбирает
`supplied_long_missions` или `synthetic_fixture`. Наличие имени и provenance ref
не аттестует подлинность корпуса: receipt всегда говорит `bank_authenticity:
not_attested`. Минимум восемь supplied tasks должны покрывать все семейства:

| family | Tags обязательных требований original mission |
| --- | --- |
| cross-layer-feature | backend, ui, storage, docs, tests |
| compatibility-refactor | producer, consumer, legacy-api |
| unknown-defect | diagnosis, reproduction, regression |
| multi-deliverable | multiple-modules, documentation, original-coverage |
| interrupted-execution | partial-result, original-budgets, reconciliation |
| upstream-repair | upstream-api, downstream-revalidation, stale-proof |
| negative-review | reproduced-finding, review-verdict |
| indivisible-outcome | transaction-boundary, handoff-cost |

Это проверка заявленного покрытия банка, не автоматическая оценка сложности
или полноты естественного языка. Tiny self-authored fixtures в tests проверяют
только механику; реальные long-mission inputs в пакет не включены.

Каждый task задаёт `id`, `family`, `source: {commit, tree}`, `mission` ref,
`public_assets`, `hidden_evaluator` ref и nullable `shared_decomposition` ref.
Все refs имеют `{path, sha256}` относительно отдельного assets root. Source
roots передаются отдельно как mapping task ID → локальный checkout; абсолютные
пути машины не входят в переносимый spec или receipt.

Mission (`task_flow_original_mission`) сохраняет точный `original_request`,
полный `requirements: [{id, text, mandatory, tags}]` и `invariants`. Каждый arm
с самого начала получает один и тот же полный mission. При `comparison:
mechanism` все arms также получают один supplied DAG; проверяются уникальность
nodes, dependencies, отсутствие циклов и покрытие mandatory requirements.
При `comparison: product` shared decomposition отсутствует у всех arms.

Hidden evaluator (`task_flow_hidden_evaluator`) связывает `task_id`, immutable
`revision`, отдельный `producer_id`, точное множество original requirement IDs
и `files` refs. Source SHA/tree и каждый raw worktree blob проверяет существующий
`capture_local_git_checkout`. Git-clean CRLF transformation, source drift,
изменение executable mode и объявленные clean/smudge, ident или encoding filters
отклоняются; filters не исполняются. Сохраняются ограничения portable paths
существующего benchmark capture, включая неподдержанные symlinks/submodules.

Prepare сравнивает hidden paths/hashes с public mission/DAG/assets, policy refs
и captured source bytes. Пересечение отклоняется до публикации. Это структурные
leak controls, а не доказательство отсутствия семантически переписанного gold
в естественном языке или произвольном коде. Независимая экспертиза банка и
реальная runtime isolation остаются отдельными обязательными qualification.

## Arms и matched requested policies

| ID | mechanism |
| --- | --- |
| A | full_prompt_tool_loop |
| B-mechanical | preplanned_mechanical_sequence |
| B-human | human_selected_sequence |
| C | supported_native_goal_continue |
| D | automatic_verified_flow |

Один `policy` фиксирует model/provider, client ID/artifact ref, tools ID/artifact
refs, permissions, общие token/cost/wall-clock budgets, root/role effort,
environment ref и cache regime. Каждый solve manifest получает его без
персонального override. Artifact refs могут указывать на pinned descriptors;
descriptor hash не выдаётся за наблюдение фактического executable/client.

`arms: [{id, configured_effort}]` содержит ровно пять разных IDs.
`configured_effort` либо null, либо ref `task_flow_configured_effort` с digest
общего policy и точными root/role effort. Несовпадение отклоняется. Manifest
разделяет requested effort, `declared_unqualified` configured values и observed
`{status: not_run, value: null}`. Нет переноса default effort в unavailable.
Возможности C и D подготовкой не доказываются: все manifests сохраняют
`capability: {status: not_run, launch_allowed: false}`. Production, isolation и
client qualification в receipt также `not_run`, execution_authorized=false.

Для каждого task/seed создаются пять циклических Latin rotations пяти arms:
каждый arm занимает каждую позицию ровно один раз. Порядок исходного ряда
определяется SHA256(seed, arm), без ambient randomness. Trial IDs связывают
spec digest, task, arm, seed, rotation и position. Это 25 planned slots на
task/seed, включая оба B arms отдельно. Repeated seed между rotations явно
виден; recipe и полное расписание заморожены, никакие trial не подбираются
после просмотра результатов.

## Публикация и проверка

Пример использования с уже предоставленными assets и восемью source mappings:

```python
from agent_harness.task_flow_experiment import prepare, verify, aggregate

receipt = prepare("experiment.yaml", assets_root="assets",
                  source_roots=source_roots, output_dir="prepared")
verify("prepared", expected_digest=receipt["preparation_digest"])
summary = aggregate("prepared", [], reports_root="future-reports")
assert summary["status"] == "not_run"
```

CLI prepare принимает repeated `--source-root TASK_ID=PATH` для каждого task:

```console
python -m agent_harness.task_flow_experiment prepare --spec experiment.yaml --assets-root assets --output prepared --source-root TASK_ID=CHECKOUT
python -m agent_harness.task_flow_experiment verify --preparation prepared
python -m agent_harness.task_flow_experiment aggregate --preparation prepared --reports future-reports.jsonl --reports-root future-reports
```

Первый пример CLI требует полного набора source-root аргументов из вашего spec;
один placeholder не является готовым банком. Aggregate принимает strict JSONL,
а spec/asset documents допускают strict YAML или JSON.

Output содержит `controller/` с оригинальными spec/input bytes,
`solve/TASK/` с исходниками, полным mission, общим DAG и arm manifests, а также
`evaluator-private/` с hidden bytes и нормализованным evaluator manifest.
Общий preparation receipt хранится в `preparation.json`. В solve manifests нет
hidden evaluator refs/digests; они доступны только controller/evaluator side.
Нельзя монтировать весь prepared root в solve. Установка permissions на private
каталоги не обеспечивает изоляцию процессов одного user; транспорт должен
квалифицировать реальные mounts и client discovery до любых runs.

Exact refs, byte sizes, executable bits, file/directory inventory, schedule и
spec/policy digests проверяются `verify`. Нормализованные bytes имеют собственные
hashes; они не заменяют original hashes и не доказывают evaluator execution.
Повтор prepare заново проверяет supplied refs/source и возвращает тот же receipt
только при полном совпадении frozen outputs. Изменённые inputs/outputs, extra
files/directories и collisions сохраняются и отклоняются. Частичная публикация
без receipt остаётся для сверки; automatic overwrite/repair отсутствует.

Лимиты: 128 tasks, 16 seeds, 10000 planned slots/files/report rows, 64 MiB
материализованных bytes, 2 MiB spec/preparation document и штатные strict parser
depth/node/string bounds. Ref и source read limits наследуются от benchmark
helpers. Превышение явно отказывает; такие результаты не называются complete.
Source и assets перечитываются перед complete receipt; это cooperative
point-in-time observation, не OS sandbox против конкурентного стороннего writer.

## Агрегация будущих отчётов

`task_flow_trial_report` привязывается к preparation digest и planned trial ID,
task, arm и candidate digest. Planner/worker/integration/reviewer/repair/retry
используют существующую `evaluation.USAGE` schema. Expected invocation IDs
декларируются явно; missing, partial, unavailable или conflicting usage не
превращается в 0. Полное coverage требует непустого exact invocation set и
полных input/output token observations. Повтор одного identical report — noop;
conflicting report и пересекающиеся invocation IDs разных trials отклоняются.

Полный mission verification учитывается только по отдельному
`task_flow_mission_evaluation` с exact original requirement IDs, pinned evaluator
bundle digest, candidate/trial/preparation binding и producer, отличным от solver.
Его `execution_evidence` обязан быть существующим benchmark `independent_grade`
receipt с теми же subject digests и согласованным outcome. Просто normalized
evaluator assets или preparation receipt не проходят эту проверку.

Строгий путь использует `independent_grade` v2, `task_flow_mission_evaluation` v2
и `task_flow_trial_report` v2. Полный subject связывает candidate capture,
frozen evaluator request, фактически перенесённый/materialized grade input,
execution и grade с wrapper/report. Проверяются
`candidate_scheme=ordinary-tree-no-git-v1` и `evaluator_input_digest`; физический root digest не
подменяет portable candidate projection. Для публикации wrapper передайте
actual execution-grade ref в `write_mission_grade`; затем передайте полученный
ref как `evaluator_receipt` в `write_trial_report`. Эти helpers выводят subject
из проверенной цепочки, сохраняя отдельно supplied usage/observations отчёта.
Legacy v1 читается как `unverified_legacy` и не увеличивает
`reported_full_mission_verified`, даже при заявленном accepted.

Проверка
локальных bytes не аутентифицирует producer и не доказывает запуск без
квалифицированного runtime: summary называется `supplied_reports_unqualified`,
сохраняет synthetic/observed-declaration distinction и никогда не выдаёт winner.

Nullable timestamps задают wall-clock interval и время independent verification;
parallel phase durations сохраняются отдельно, не суммируются как elapsed time.
Missing trials, timeouts и infrastructure errors остаются в planned denominator.
Summary отражает mandatory pass/fail/unknown, false completion/unknown, manual
interventions, human minutes/feedback, time-to-verified и observed usage/coverage.
B-human feedback — дополнительная информация, её не выдают за matched input.
Future `observed_policy` nullable ref отдельно сохраняет actual role observations,
coverage и runtime evidence refs; mismatch requested policy учитывается отдельно,
не исправляется defaults. Subscription credits из tokens не вычисляются.

Пустая агрегация — `not_run` с unknown usage. Реальные long-task quality, client
capabilities, restart/compaction recovery, Windows и production isolation этим
preparation этапом не квалифицированы. Полный платный A/B эксперимент не выполнен.
