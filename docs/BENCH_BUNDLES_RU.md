# Локальные task bundles и собственный corpus

`benchmark_bundles.LocalTaskBundleAdapter` реализует concrete materialize/evaluate путь поверх существующих `BenchmarkRunner`, `CodexAdapter` и `Supervisor`. Это custom protocol `artifact-json-v1`: pinned исходники и JSON-артефакты с независимыми versioned ответами. Он не является raw importer или официальным evaluator SWE-bench, Terminal-Bench либо Aider и не создаёт leaderboard score.

`benchmark_corpora.export_own_corpus(destination)` без сети и model calls экспортирует шесть задач версии `harness-custom-corpus-v1`, source refs, oracle refs, bridge, adapter source, toolchain profile и индекс. Возвращённые два suite descriptor можно включить в frozen manifest. Три внешних suite требуют настоящих pinned assets и своего явно выбранного протокола; экспорт не заполняет их выдуманными SHA. Объект corpus в исходном модуле версионирует вопросы, исходники, ответы и fault schedule; индекс содержит его digest.

| Task | Независимый oracle |
|---|---|
| pending-wave | Оставшийся DAG и однократные известные token totals |
| unknown-outcome | Не повторять неизвестную invocation; partial usage |
| related-tests | Физические path/line ranges связанного теста |
| trace-to-code | Упорядоченные реальные source frames |
| change-impact | Прямые и транзитивные callers, связанный test |
| symbol-context | Definition/callee spans без постороннего кода |

Это semantic/application corpus, а не закрытие всех F-regressions и не доказательство реального compact/resume. После исследования каждый task публикует `checkpoint.json`, затем `answer.json`. Запрос и начальные source-файлы копируются в `trial/source`; oracle/expected data — отдельно в `trial/evaluator`. Следующие A/B trials получают отдельные каталоги. Protected inventories, physical parent bindings и receipt SHA проверяются до transport/evaluator; receipt SHA входит в frozen work contract. Допустимые output-изменения отделены от immutable inputs. Gold не включается в acceptance-команды, видимые solve: они проверяют только JSON-формат. Независимый subprocess сравнивает с gold, сохраняя различие `true` и `1`, отклоняет duplicate keys, неподходящие bytes и неизвестные outputs. Model output никогда не выбирает исполняемую команду evaluator.

Материализация standalone через `materialize_task_bundle(...)` готовит только bytes и receipt. Повторная или прерванная materialization не перезаписывается; её состояние сохраняется для диагностики. Закрытые схемы находятся в `schemas/benchmark-{task-bundle,artifact-oracle,materialized-task,milestone}.schema.json`. Внешние normalized bundles могут использовать этот data-only API с `provenance: normalized_external`; это означает custom evaluation, не официальный split.

Для driver нужен capability-объект `TaskBundleBoundary`: он предоставляет pinned identity, fresh control argv через solve boundary, существующий Codex transport, поддержанный upper bound **для всей trial**, отдельный evaluator route и реальные metric producers. Это технический API, не новый approval flow. Переданные локальные assets уже входят в разрешённую задачу. Авторизация расхода остаётся существующим явным `BudgetAuthorization`, связанным с frozen manifest. Model/provider/effort и original deadline не изменяются.

Default boundary возвращает `ENVIRONMENT_ISOLATION_UNAVAILABLE` до materialization/model calls. Production observed registry по-прежнему пуст; synthetic boundary tests не квалифицируют production. Физически разные каталоги сами по себе не запрещают соседний доступ тому же OS user. Для real run необходимо настоящее filesystem/network/evaluator/peer-state разграничение с fresh controls и отдельным разрешённым model transport. В текущей среде Docker socket запрещён; обход не предусмотрен.

Вход отдельного evaluator закрыт схемой `benchmark-grade-input.schema.json`: version, manifest/trial/evaluator/solve digests, cwd и execution outcome. `accepted` не является допустимым execution outcome; его выводит только независимая проверка артефактов. Unknown execution блокирует grading и требует reconciliation.

`benchmark_milestones.MilestoneObserver(receipt, controller_state)` наблюдает ordered JSON-предикаты task artifacts, а не номера model calls. `observe()` связывает milestone с exact trial/manifest/materialization и candidate inventory. `claim_fault(id)` публикует durable intent до действия внешнего controller. После crash повторный claim запрещён, исход необходимо явно reconciled через `reconcile_fault(operation_id, outcome)`; unknown не становится applied. State хранится вне solve-каталога. API не убивает и не возобновляет Codex. Live fault injection с безопасной сверкой usage, actual controller restart, compact/resume и сопоставимыми milestones в обоих arms остаётся отдельной обязательной интеграцией; один receipt не выдаётся за её выполнение.

Readiness проверяет все шесть oracle, path/hash/closed-schema controls, immutable/protected data, legitimate output edits, observable milestones/unknown reconciliation и concrete persisted driver с явно synthetic transport. Model calls реального клиента — `0`. Full official cohorts, stock client/home isolation, observed role-policy mapping, production upper bounds/metrics и actual environment qualification остаются незавершёнными. G4 и BENCH real evaluation остаются `NOT_RUN`; исправный offline runner не превращает их в PASS.
