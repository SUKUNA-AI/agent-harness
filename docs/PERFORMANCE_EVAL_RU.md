# Метрики и эксперименты — 1.6.0

[Baseline](BASELINE_1_5_0.md) · [Источники](SOURCES_AND_EVALS.md) · [Memory/context](MEMORY_AND_CONTEXT_RU.md)

Harness ускоряет только те части работы, для которых есть сопоставимое
измерение. Correctness tests, меньший JSON или наличие пяти workers не
доказывают end-to-end выигрыш. До завершения реального A/B результата
производительности соответствует статус `insufficient_data`.

## Определения

| Метрика | Интерпретация |
| --- | --- |
| Time to acceptance | Wall-clock от начала до актуального accepted; для неуспеха отсутствует |
| Accepted/total | Task outcome по independently checked evidence |
| Input/output tokens | Usage из native events; missing/partial usage обозначается отдельно |
| Turns/repairs | Initial и последующие подтверждённые model phases |
| Controlled/observable tool calls | Вызовы harness и наблюдаемые Codex events учитываются отдельно от скрытых tools |
| Duplicate reuse/revalidation/cache | Отдельные counters; suppressed call не равен saved tokens |
| Spawn/integration overhead | Локальное время запуска/объединения, включая conflicts |
| Memory staleness | Случаи, когда старая запись исключена из verified recall |
| Recovery | Подтверждённые interrupted/reconciled attempts; restart не обнуляет failure history |

Wall-clock фаз может перекрываться: их сумма не обязана равняться времени до
приёмки. Если стоимость в валюте не измерена по зафиксированным тарифам,
report оставляет cost `unavailable`. Встроенные hidden tools не учитываются
как наблюдённые. Нулевой счётчик означает отсутствие зарегистрированных
событий этого типа, а не доказательство отсутствия любых hidden operations.

`spawn_seconds` интерпретируется вместе с `spawn_measurements` и
`spawn_overhead_status`. Без measurements прежний ноль не означает мгновенный
spawn. Worktree setup и integration имеют отдельные counters; slot wait входит
в общий wall-clock и отдельно сейчас не измеряется. Partial
usage не является полным расходом; отсутствие цены оставляет cost unavailable.

## Сопоставимый A/B

1. Зафиксируйте actual base SHA, candidate SHA, source fixture, OS/hardware,
   Codex version, model/profile/reasoning, permissions и compaction.
2. Сохраняйте одинаковые goal, acceptance evaluator, budgets и relevant tools.
   Изменяйте один механизм: Reducer, Ledger, conditional RepoMap или TOON.
3. Сравните single/managed 2/3/5 отдельно от native mode. Включите planner,
   model turns, worktree merge, checks и manual interventions.
4. Используйте несколько задач и repeats. Покажите sample size, failed cases,
   variance и ограничения replay; не выбирайте только лучший запуск.
5. Отклоните оптимизацию при снижении acceptance/quality, stale reuse,
   потере diagnostics, permission escalation или необъяснимой latency regression.

Задачи должны включать symbol lookup, known-defect fix, cross-file change,
test addition, integration failure и recovery на малом/среднем/большом дереве.
Предварительные fixed fixtures не заменяют неизвестный реальный codebase.
Матрица эксперимента и сохранённые receipts должны различать fake adapter
regression tests и настоящие model runs.

Token saving считают на сопоставимых принятых задачах:
`(baseline_tokens_per_accepted - candidate_tokens_per_accepted) / baseline_tokens_per_accepted`.
Baseline failure не даёт основание объявлять token win candidate. Выигрыш из
paper не является результатом этого harness.

## Выборочное TOON

[`format_bench.py`](../src/agent_harness/format_bench.py) реализует узкую
tabular форму uniform flat scalar rows. JSON остаётся default для nested,
heterogeneous и непроверенных данных. TOON не используется для состояния,
JSONL или structured-output schemas.

```text
python tools/harness.py work format-bench --input TABLE.yaml --output BENCH.yaml
python tools/harness.py work format-bench --input TABLE.yaml --real-codex --repeats 5 --cwd /absolute/isolated-repo --evidence-dir /absolute/private/format-evidence --output MEASURED.yaml
```

Вход — list одинаковых flat objects в Strict YAML/JSON subset. Первая команда
сравнивает payload bytes и честно возвращает `insufficient_data`: bytes не
подменяют token usage. Вторая выполняет одинаковую lookup-задачу в трёх
представлениях JSON/Markdown/TOON с независимым exact-answer evaluator.
Пять repeats означают **15 последовательных Codex calls**. Учитывайте общий
model-session budget перед запуском. Команда не задаёт другую модель, provider
или reasoning; установленная конфигурация наследуется. Используется read-only
Codex с локальным отключением native children.

Benchmark учитывает format instructions, observed `turn.completed.usage`,
correctness, CLI completion и exact session IDs. Private evidence содержит
sanitized JSONL и typed YAML samples. Missing usage не превращается в 0.
`--output` создаётся exclusive; для следующего отчёта выберите новый файл.

Локальная selection policy требует хотя бы пять paired samples, nominal
односторонний sign-test `p≤0.05`, median input saving≥5%, меньший payload и
отсутствие ошибок одинакового evaluator. Это консервативный exploratory gate
для конкретного набора bytes/model/environment, не универсальная гарантия
статистического превосходства. Повтор одной deterministic таблицы особенно
ограничивает переносимость вывода на другие задачи.

`select_format` заново проверяет samples и принимает TOON лишь при совпадении
data digest, shape, model label, output type и environment fingerprint.
Negative benchmark, quality regression, missing usage или новая структура
выбирают JSON. Ни один произвольный `enabled: true` без samples не достаточен.

## Наблюдённый pilot 1.5.0

На frozen pilot candidate `5cdf3d3` проведены 5 repeats для каждой из трёх
форм: 15/15 exact answers correct. Median paired input saving TOON около 0.71%,
one-sided sign-test p=0.1875; результат `measured_negative`, выбран JSON.
Это одна lookup-таблица и диагностический эксперимент, не общее сравнение моделей.
Receipt без matching environment fingerprint не включает TOON задним числом.

Контрольный scheduler cap1/2/3/5 на contained fake processes проверяет overlap,
изоляцию, интеграцию и acceptance mechanics. Он не измеряет LLM quality,
model latency или token throughput. Repetitive/error packet byte reductions
и cache counters — локальные proxy. Causal end-to-end performance остаётся
`insufficient_data`, пока нет сопоставимого real baseline/candidate A/B.

## Как читать результаты релиза

Исходные проверки опубликованы отдельно в [baseline](BASELINE_1_5_0.md).
Они подтверждают выполненные команды на исходном checkout и платформе,
не ускорение 1.5.0. Финальный release report должен приводить actual test
statuses, число real model samples, mode/cap, evidence hashes и residual
limitations. Required `not_run`, `skipped`, `unknown` не являются passed.

Сохранённый compaction threshold `500000` — пользовательская политика
сжатия active history. Он не увеличивает физическое окно модели и не
ограничивает суммарные токены задачи. Budget enforcement и наблюдаемость
usage описывайте по актуальному runtime; не выдавайте schema field за
гарантированный hard backend limit.

## Applicability и portfolio в 1.6.0

Alias installed/null fingerprint не разрешает automatic TOON. Receipt v2
связывает route/config, client binary/build, evaluator/template, data/shape и
complete samples с отдельными уникальными session IDs. Synthetic statistics
не включают production selection; complete raw evidence проверяется при reuse.
Срок — один день в observed identity scope; скрытые server-side model updates
не объявляются детектируемыми. Render требует исходный `--evidence-dir`.

Offline `work benchmark` валидирует frozen local portfolio, задаёт paired
schedule и считает полные denominators, missing/failed outcomes, usage coverage
и task-cluster uncertainty. Qualified model runner, official pinned assets,
operational isolation, approved total budget и реальные runs остаются отдельным
незавершённым этапом; synthetic report не подтверждает экономию.
[Полные ограничения](STABILIZATION_RU.md).

Report сохраняет `accepted_pairs.per_task`: каждый task/repeat, исходы обеих
ветвей, наблюдённые и полные tokens, absolute/relative savings и причину
неопределимого процента. Failed/missing/partial observations остаются видны.
`costs` отдельно суммирует наблюдённые затраты принятых в обеих ветвях пар с
coverage; `complete_usage_costs` содержит сопоставимые полные затраты.
Нулевой baseline сохраняется в этих totals, его индивидуальный процент — null.
Отношение totals обозначено `relative_token_saving_of_totals`; среднее repeats
по task и median task clusters имеют отдельные поля. `full_cohort` включает
также неудачные запуски и не заменяется выборкой accepted pairs.

Format resolver подтверждает только закрытый профиль isolated table lookup.
Unknown config sections, dynamic hooks/MCP/plugins, ambient AGENTS или explicit
project cwd не имеют здесь доказанной полной closure и отключают automatic
positive applicability. Данные не читаются для извлечения private prompt; для
такого профиля остаётся JSON. Features/agent scalar settings включены в fingerprint.

## Persisted cohort driver API

[`BenchmarkRunner`](../src/agent_harness/benchmark_runner.py) принимает frozen
manifest/assets, отдельный private run store, подготовленный adapter и explicit
`BudgetAuthorization(manifest_digest, authorization_id, max_total_tokens, deadline,
measurement_kind)`. `create`, `advance`, `run`, `status`, `report` используют
существующий journal, Supervisor для harness arms и CodexAdapter для plain arms.
Полное расписание и reservations фиксируются до dispatch. Unknown solve/grade
блокирует replay; complete receipts повторно не запускают модель. Failed/timeout
trials остаются в первоначальном знаменателе. Caller authority не читается из
недоверенного поля manifest `approved`.

В текущей поставке production isolation adapters не зарегистрированы: обычный
наблюдаемый model run возвращает `ENVIRONMENT_ISOLATION_UNAVAILABLE` до dispatch.
Contained synthetic controls проверяют driver interface и не квалифицируют
реальный filesystem/network/evaluator boundary. Для actual cohort нужен отдельно
проверенный поддержанный environment adapter; пустой registry нельзя заполнять
caller flag или текстовым заверением. Budget/hard-cap guarantees остаются разными.

Trial receipts v2 содержат nullable `metrics` и `phase_seconds`; v1 читаются
по прежней закрытой схеме. Ненаблюдаемые manual interventions, пропущенные узлы,
duplicate writes и overhead имеют null/unavailable. Ненулевое значение и
наблюдённый ноль требуют отдельного metric receipt с trial/manifest binding,
producer scope и hashes исходных instrumentation records. Report проверяет эти
ссылки и показывает unknown/partial coverage отдельно. Байтовая целостность
метрик не квалифицирует producer или изоляцию среды.

Текущие trial v1/v2 не содержат наблюдённую role-level model/effort map клиента.
Поэтому observed matched comparison сохраняет descriptive costs, но возвращает
`insufficient_data`/`matched_role_mapping_unavailable`. Равные policy hashes не
доказывают одинаковое фактическое распределение reasoning. Synthetic accounting
не является реальным causal comparison; package comparison имеет отдельный scope.

Adapter может предоставить `independent_grade` для закрытой evaluator boundary.
Этот callback — единственный путь данного grade dispatch: после его неизвестного
исхода нет fallback на argv или повторного evaluator call. Старый pinned argv
протокол сохранён для adapters без callback. Fault-controller hooks вызываются
перед solve и после сохранения точного solve artifact; default их не создаёт.
