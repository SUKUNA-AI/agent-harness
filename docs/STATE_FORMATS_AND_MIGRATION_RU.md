# Форматы состояния и миграция — 1.5.1

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
| Recovery journal | Небольшие typed JSONL records с hash chain | Не пользовательский чат; replay только complete verified records |
| Existing core run, evidence, release/install manifests | Исторический JSON | Existing reader/rollback продолжают работать |
| Architecture narrative и derived knowledge | Markdown | Authoritative structured provenance остаётся в YAML |
| Optional model table payload | TOON после измерения | Никогда не подменяет state, schema или native JSONL |

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

Installation/rollback manifests остаются исходными: пользовательские settings,
marker blocks, backups и active manifest chain обслуживает штатный installer.
Перед обновлением выполните doctor и install dry-run, проверьте targeted diff,
затем примените и вызовите verify. Для native loading нужен отдельный новый
Codex process; изменение файлов не гарантирует hot reload текущего чата.

Portable package содержит schemas/parser/license, но не local machine paths.
Проверьте extracted distribution через isolated homes, upgrade и rollback;
ZIP source archive без native bundle не подтверждает Rust capability.
