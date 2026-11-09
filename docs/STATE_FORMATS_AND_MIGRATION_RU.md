# Форматы состояния и миграция — 1.6.0

[Supervisor](EXECUTION_SUPERVISOR_RU.md) · [Установка](../SETUP_RU.md) · [Memory](MEMORY_AND_CONTEXT_RU.md)

Новые task contracts, supervisor snapshots/checkpoints, agent state и память
сохраняются в Strict YAML. Старые core run/evidence и installer manifests
сохраняют собственный JSON-формат. Совместимость не требует переписывать
исторические receipts или настройки Codex.

| Данные | Канонический формат | Примечание |
| --- | --- | --- |
| Установленный Codex | Его `config.toml` | Адресный обратимый installer; model/provider/permissions сохраняются |
| Work contract/state/checkpoints/agent/memory | Strict YAML, независимый `schema_version` | Закрытые schemas, bounded fields и UTC timestamps |
| Codex process events | JSONL native protocol | Хранятся отдельно; model completion не равно acceptance |
| Recovery journal | Небольшие typed JSONL records v1/v2 с hash chain | Root preparation/reservation facts требуют v2; default events/checkpoints остаются v1 |
| Existing core run, evidence, release/install manifests | Исторический JSON | Existing reader/rollback продолжают работать |
| Architecture narrative и derived knowledge | Markdown | Authoritative structured provenance остаётся в YAML |
| Optional model table payload | TOON после измерения | Никогда не подменяет state, schema или native JSONL |

## Journal и root preparation

Reader `supervisor-journal-event.v2.schema.json` читает смешанную hash chain
outer versions 1/2. Старые schema file и inline const1 сохранены. Только закрытые
root preparation/reservation/failure payloads используют outer2; root payload1,
обычный payload2 и неизвестные будущие версии отвергаются. Первый authoritative
root protocol event блокирует старого writer также для supervised native/legacy
run. Это не включает plan quality и не добавляет keys в work-state/receipt.

Core preparation intent ссылается на immutable addressed metadata и отдельные
JSON blobs contract/baseline/prior/expected state. Метаданные и journal row
сохраняют предел2MiB, journal —64MiB. Каждый blob ограничен100MiB+2bytes, как
существующий native snapshot response; Python-only inventory выше этого предела
получает явный capacity stop. Большой core snapshot проверяется существующим
snapshot validator/digest, а не YAML node limit. Hashes, current subject,
origin baseline и physical ownership повторно проверяются при recovery.
`publication_prior_ref` закрепляет прежний state inode и существующие parents
до intent. Fact `root_core_preparation_files` публикует addressed manifest всех
staged output identities до их установки. Проверяются file inodes, hashes и
вложенные parents; same-bytes replacement не разрешает overwrite или reuse.
Обычные core JSON/CLI limits не меняются. Подробности transaction ordering и
typed outcomes — в [Supervisor](EXECUTION_SUPERVISOR_RU.md#подготовка-root-repair-и-reservation).

## Strict YAML

[`strict_yaml.py`](../src/agent_harness/strict_yaml.py) использует vendored
PyYAML **6.0.3**, MIT; version/license manifest включён в пакет. Online parser,
обязательная внешняя Python installation или JS runtime не требуются.

Запрещены arbitrary tags, anchors/aliases, merge keys, multiple documents,
duplicate/non-string keys, non-finite numbers и неизвестные поля закрытой
схемы. Scalar conversion соответствует JSON spelling: `true`, `false`, `null`
и JSON numbers. `yes`, `off`, даты и leading-zero числа остаются строками;
schema не даст принять строку как boolean/integer. Не полагайтесь на implicit
YAML conversions. UTC timestamp проверяется явно.

Parser ограничивает размер документа 2 MiB, глубину 48 и число nodes 100000.
Отдельные schemas ужесточают длины, arrays, paths и enums. Неизвестная будущая
`schema_version` даёт информативный отказ, а не silent downgrade.
[`schemas/`](../schemas/contract.schema.json) содержит как legacy schemas, так
и `work-contract`, `work-state`, `agent-plan`, `agent-state`, `ledger-event`,
`memory-*`, `check-cache*` и `format-*` схемы текущего слоя.

Check cache/proof используют `schema_version: 3`; legacy v1/v2 даёт cache miss
без удаления или переписывания оригиналов. `input-closure` v1 описывает полный
ограниченный physical inventory, включая ignored/hidden files и directories.
`python-runtime-closure` v1 отдельно связывает finite startup/DLL/stdlib inputs
и отсутствие альтернативной startup configuration.

Typed work `inputs` состоит из absolute ordinary-file paths и optional SHA-256;
оно передаётся в совместимый frozen contract. Не заменяйте file ref каталогом
или boolean `complete`: это не доказывает полный набор dependencies. Cache v3
проверяет отдельные `input-closure` и `python-runtime-closure` bounds; runtime
policy поддерживает только проверяемый self-contained Windows literal bundle.
Обычный interpreter не становится пригодным для reuse от явного `pure: true`.

## Private layout

Выбирайте private state вне source checkout. Не помещайте реальные runs,
credentials, machine pointer или model transcripts в release/Git source.

```text
<state>/
  global-model-slots/       OS guards + ownership receipts
  runs/<run>/
    contract.yaml
    state.yaml
    checkpoints/
    journal/events.jsonl
    locks/
    codex/                  separate sanitized native streams
    verification/           existing core JSON run + immutable evidence
    agents/
    plans/
    worktrees/              managed staging and owned workers
    patches/
    acceptance/report.yaml
    reports/report.yaml
    reports/summary.md
    context/handoff.yaml
    ledger/events/*.yaml
    ledger/index.yaml
  memory/<repository-identity>/
    short_term/
    episodes/
    semantic/
    index.yaml
    knowledge/              derived Markdown + relations.yaml
```

Это схема типичного work run; optional directories появляются по необходимости.
`--run-dir` и `--state-dir` могут указывать другие постоянные private paths.
Для общей managed quota все участвующие foreground runs используют один state
root. Defaults берутся из installation pointer, затем platform conventions;
явные paths упрощают аудит и перенос.

## Атомарность, locks и recovery

Private IO отказывает на symlink/reparse/FIFO и неподдерживаемых типах.
Writer делает staging в том же каталоге, flush/fsync и atomic replace; на POSIX
синхронизируется также parent directory. Snapshot generation и journal hash
chain позволяют обнаружить torn/corrupt записи. Они не превращают неизвестный
исход model write в successful task.

Single-writer lease проверяет process creation identity, не только PID.
Повторный resume сначала reconciles actual process/source/session. Повреждённый
полный journal record требует диагностики. `work resume --repair-journal`
может архивировать лишь доказанно incomplete последний append; затем остаётся
обязательная reconciliation. Не удаляйте lease/checkpoint вручную, чтобы
заставить accepted появиться.

## Legacy compatibility и миграция

### Полнота usage после аудита

В `work-state` добавлено optional `metrics.usage_accounting_version: 2`.
Новая версия означает, что каждый наблюдённый turn оценивался на полноту обоих
token counters. Старое состояние без поля по-прежнему читается; schema version
не меняется, исходные checkpoints/receipts не переписываются автоматически.

Старые totals не доказывают отсутствие пустого usage в одной из прошлых
попыток. Поэтому при заданном `max_tokens` legacy history без accounting v2
не разрешает новый spawn или повторное подтверждение приёмки. Read-only
`status --verify-evidence`/accepted resume возвращают `STALE`, сохраняя
`recorded_status: ACCEPTED` в историческом результате. Нельзя просто дописать
маркер вручную: он не восстанавливает отсутствующую статистику. Новый полный
turn не делает старую историю проверенной. Без token cap это ограничение не
блокирует legacy run; totals помечаются `unverified_legacy`, если они есть.
История, в которой ещё вообще не было model execution или usage, может начать
accounting v2 при первой наблюдённой попытке. Настоящий `0/0` считается полным.

### Существующие core и installation данные

Команды `init`, `attempt`, `freeze`, `check`, `evidence`, `gate`, `checkpoint`,
`resume`, `status`, installer verify/rollback продолжают работать с legacy
JSON. Namespace `work` добавлен отдельно. Supervisor создаёт compatibility
core run в `verification/` и использует существующую acceptance реализацию.

Для собственных структурированных данных доступна явная функция:

```python
from agent_harness.strict_yaml import migrate_json

receipt = migrate_json("old-record.json", "new-record.yaml", schema)
```

Здесь `schema` — соответствующая закрытая schema, а не произвольный тип.
Source читается с duplicate-key checks, валидируется и сохраняется в новом
exclusive destination. Старый JSON не удаляется; существующий YAML не
перезаписывается. Эта функция не объявляет любой legacy run новым work run и
не конвертирует installation chain автоматически.

Штатный installer обслуживает пользовательские settings, marker blocks,
backups и active manifest chain. Существующие installation/rollback manifests
сохраняют свои versioned formats.
Новые installer manifests v3 используют `commit_protocol: active-binding-v1`.
Commit подтверждает проверенная active manifest chain либо сохранённый
`installs/<id>/commit.json` с exact manifest/active-pointer/physical binding;
один status label не доказывает commit. При неоднозначном владении сохраняются
backups и чужие edits, а recovery требует сверки. `migrate_json` не заменяет
этот installation protocol и не повышает trust старых manifests.
Перед обновлением выполните doctor и install dry-run, проверьте targeted diff,
затем примените и вызовите verify. Для native loading нужен отдельный новый
Codex process; изменение файлов не гарантирует hot reload текущего чата.

Portable package содержит schemas/parser/license, но не local machine paths.
Проверьте extracted distribution через isolated homes, upgrade и rollback;
ZIP source archive без native bundle не подтверждает Rust capability.

## Work-state v2 и explicit migration

Work/agent state v2 ввели durable execution receipts и usage accounting v3.
Текущий новый managed run с work-contract v2 создаётся как work-state v3 с
plan-quality policy; agent projections и другие work paths остаются v2.
Contract/plan имеют собственные версии v2. Legacy work-state v1/v2 читаются без
автоматического upgrade или повышения trust.
Execution receipts имеют собственную закрытую schema; usage accounting v3
подтверждает invocation coverage, а не одно наличие чисел.
`work migrate` сначала показывает hash-bound план; apply проверяет expected
generation, сохраняет backups/intent и публикует pending versioned barrier.
Pending блокирует supervisor; `--recover` явно завершает переход.
`legacy_history_unverified` не снимается новой успешной попыткой.
[Команды и downgrade ограничения](STABILIZATION_RU.md).

## Явный переход managed work-state v2 → v3

`work migrate --run-dir RUN --expected-generation N --target-version 3`
строит read-only план установки plan-quality policy. Исходный run должен иметь
managed contract v2, согласованные checkpoint/journal и остановленные execution
invocations. Проверяются W03 cleanup/subject/accounting guard и registry данного
run; неизвестный исход или отсутствующий registry после использованных попыток
требует отдельной reconciliation. Миграция не вызывает модель и не освобождает
неопределённые slots. Просроченный deadline не разрешает начать новый upgrade.

План schema2 связывает generation, точные source bytes/hash inventory, journal
head, снимки source roots, material digest и versioned policy/schema refs.
Используется committed `state.contract_path`, включая successor mission;
исходный `contract.yaml` не подменяет текущую миссию. `--max-planning-rounds N`
может только уменьшить предел из исходного `contract.budgets.max_attempts`;
для apply нужно повторить тот же параметр и передать reviewed `--expected-plan-digest`.

Apply сохраняет ограниченные backups и immutable intent schema2 с точными
pending/final state digests и frozen bootstrap material. Сначала публикуется
authoritative v3 pending barrier; только затем policy/schema artifacts и их
journal pins; последним — final v3 head и delivery receipt schema2. Старый v2
writer отказывает на новом checkpoint. Pending блокирует обычный supervisor;
`--target-version 3 --recover OPERATION_ID --expected-generation N` явно завершает
сохранённый переход. Recovery проверяет backups/source/pending/final binding,
повторно публикует только точный frozen material и восстанавливает недостающие
derived projections. Даже после истечения deadline разрешено закончить это
housekeeping без model calls или увеличения бюджета. `--max-planning-rounds`
при recovery не принимается: новый пакет не пересчитывает сохранённую policy.

Переход сохраняет исходные deadline, attempts, budgets, usage/trust и failure
history, старые receipts, selected plan и candidate/core refs. State становится
`RECOVERY_REQUIRED`, plan_quality — `unassessed`, rounds_used — 0 и assessment_ref —
null. Это не приёмка старого плана или старого reviewer verdict: перед новой
работой нужен новый bound assessment в оставшихся исходных лимитах. Если run
ещё не имел completion policy v2, она публикуется за тем же v3 barrier;
исторические review receipts продолжают читаться по своим frozen versions.

Default-маршрут `work migrate` по-прежнему выполняет только v1 → v2 и сохраняет
прежние migration plan/intent/receipt schema1. Явный target3 не объединяет два
перехода автоматически. Read-only inspection/status и обычный resume legacy run
не устанавливают policy/schema и не повышают версию. Unknown versions отказывают
закрыто. Backups не являются разрешением на downgrade или ручную замену journal.

Лимиты migration inventory: 512 файлов и 32 MiB суммарных source bytes; отдельный
structured document ограничен 2 MiB. Превышение или конфликт прекращают переход,
а не обрезают историю. Hash bindings проверяют согласованность локальных bytes;
они не удостоверяют автора receipts и не обеспечивают OS isolation.
