# Сравнение длительных workflow

Это протокол подготовки эксперимента. Реальные платные model runs выполняются
отдельно. Наличие prepared task bank и зелёных fixtures не квалифицирует модели,
client isolation, native compaction/restart или превосходство harness.

Четыре arms получают одинаковый полный mission с начала:

| Arm | Управление | Дополнительный источник информации |
| --- | --- | --- |
| A full prompt | Штатный tools loop, полный backlog сразу | Нет |
| B mechanical sequential | Выдача заранее подготовленных связных контрактов | Нет |
| B human sequential | Человек выбирает следующий этап и проверяет результат | Уточнения и human minutes учитываются отдельно |
| C native autonomy | Поддержанный client goal/continue loop | Возможности клиента фиксируются фактически |
| D verified flow | Bounded work + durable node/integration gates + final coverage | Те же исходные условия и hidden evaluator |

Mechanism comparison даёт всем одинаковый curated DAG. Product comparison даёт
только исходный запрос, а decomposition оценивает отдельно. Отчёт не смешивает эти
два вопроса. Requested/configured/observed role effort записываются отдельно;
unknown нельзя достраивать по defaults или одному policy hash.

Минимальный scenario bank должен содержать следующие семейства:

| Семейство | Обязательные outputs/invariants | Что оценивает независимый oracle |
| --- | --- | --- |
| Cross-layer feature | backend/UI/storage/docs/tests | Фактический сквозной сценарий и отсутствие stubs |
| Compatibility refactor | producer/consumer/API migration | Старые клиенты и новые interactions |
| Unknown defect | bounded diagnosis + reproduced fix | Причина и regression, а не угаданный патч |
| Multi-deliverable mission | несколько модулей и документация | Полнота исходного запроса при слабом общем smoke |
| Interrupted execution | частичный результат + original budgets | No duplicate writer и безопасное reconciliation |
| Upstream repair | новое API + downstream revalidation | Stale proof до актуальной проверки |
| Negative review | воспроизводимая находка reviewer | Completion не заменяет положительный verdict |
| Indivisible outcome | одна связная транзакционная граница | Стоимость лишнего дробления/handoffs |

До solve замораживаются source SHA/tree, dataset/evaluator revision, исходная
спецификация и независимые hidden критерии. Gold changes/hidden tests доступны
только evaluator. Task bank не выбирается после просмотра результатов arms.
Для всех arms фиксируются client/model/provider IDs, tools/permissions, общие
token/cost и wall-clock budgets, root/child policies, environment и cache regime.
Непроверенный adapter/isolation transport закрывает qualification, а не заменяется
fake capability receipt. Существующий production registry остаётся fail-closed.

Повторы randomize/counterbalance по задачам и seeds. Human feedback B-human
сохраняется как дополнительная информация; отдельный replay control может дать его
остальным arms. Planner/reviewer/repair, timeouts и infrastructure failures входят
в denominator и общий расход. Время параллельных phases не суммируется как wall time.

Основные метрики: full mission acceptance, mandatory requirement coverage,
false completion, requirement omission, integration defects, manual interventions,
time to verified result, total observed model usage с coverage/unknown, recovery
correctness, context overhead и verification overhead. API usage не переводится
в subscription credits произвольным коэффициентом. Статистика парная по задачам,
с uncertainty по task clusters. Один showcase не доказывает устойчивый эффект.

Абляции D: без intermediate verification, без pinned mission, без final original
requirements audit; rigid против adaptive granularity; fresh против preserved
context; one worker против независимых lanes; equal-budget extra thinking для A/C.
Численный quality/noninferiority и допустимая цена/latency фиксируются до runs.
При превосходстве качества за счёт большей цены публикуется tradeoff.

Journal scaling измеряется отдельно от model quality: 1k/10k appends/commits с
активным validator, числом повторно разобранных rows и fresh full replay. Это не
end-to-end model speedup. Hook latency, inventory hashing, worktrees, model time,
checks и context reconstruction требуют раздельных timings в реальных runs.

Исполняемая подготовка supplied bank и проверка будущих reports: [task-flow preparation](TASK_FLOW_PREPARATION_RU.md), `work benchmark task-flow-experiment`. Реальный long-mission corpus не входит в package: synthetic fixtures проверяют только driver mechanics.
