# Метрики и эксперименты — 1.5.1

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
