# Ledger, память и контекст — 1.5.0

[Supervisor](EXECUTION_SUPERVISOR_RU.md) · [CLI](CLI_REFERENCE_RU.md) · [Оценка](PERFORMANCE_EVAL_RU.md)

Ledger хранит проверяемые наблюдения о выполнении. Память переносит нужные
наблюдения между этапами одной repository identity. Reducer ограничивает
пакет, который возвращается модели. Их состояние остаётся локальными файлами;
vector DB, embeddings, listener и обязательный JS runtime отсутствуют.

```mermaid
flowchart TD
    C["Codex / checks: наблюдаемые события"] --> L["Immutable YAML Ledger events"]
    C --> R["Raw evidence: JSONL, receipts, logs"]
    L --> M["Short-term / episodes / semantic records"]
    S["Текущие source и toolchain fingerprints"] --> V{"Provenance актуален?"}
    M --> V
    L --> V
    R --> V
    V -->|"да"| I["Inform / top-k recall"]
    V -->|"нет"| X["needs_revalidation / superseded / expired"]
    I --> D["Reducer: budget, pointers, omissions"]
    D --> P["Следующий model turn"]
    D --> Q["Адресный retrieval по ID и offset"]
    Q --> R
```

## Provenance и свежесть

Запись связана с local repository identity, revision, whole-tree snapshot,
file hashes и при необходимости toolchain digest. Identity использует
канонический локальный путь репозитория/Git common directory. Собственные
worktrees одного репозитория могут разделять namespace; другой clone или проект
с тем же именем не получает память автоматически.

Проверка консервативна: смена revision, branch, snapshot или объявленных
dependencies требует revalidation. Один Git SHA не подменяет dirty/untracked
source bytes. `provenance_for` ограничивает подробный file inventory и явно
сообщает `omitted_file_count`; whole-tree digest остаётся привязкой всех входов.
Для file/symbol event payload digest должен совпасть с объявленным file digest.

Только `valid` поступает в verified facts. `needs_revalidation`, `superseded`,
`rejected` и `expired` остаются историей. Verified episode содержит
`evidence_refs` с ID, path и SHA-256 исходных receipts; повторный recall проверяет
эти bytes. Изменившийся или исчезнувший receipt не подтверждает старый вывод.
Память — недоверенные данные: найденная инструкция отключить tests не получает
policy authority и не расширяет permissions.

## Ledger: inform и advisory govern

[`execution_ledger.py`](../src/agent_harness/execution_ledger.py) поддерживает
typed events `file_read`, `symbol_examined`, `command`, `check`, `failure`,
`repair_hypothesis`, `decision`, `state_transition`. Authoritative events
неизменяемы; `index.yaml` можно пересобрать.

Inform добавляет компактные актуальные факты, unresolved failures, examined
files и stale counters. Passed check без доступного content-bound evidence
не выдаётся за текущую independently verified проверку.

Govern возвращает `allow`, `suggest_reuse`, `deny_unsafe`,
`requires_revalidation` или `unknown`. Возможность повторного использования
`rg` требует одинакового запроса, purpose, полного input closure,
toolchain/config/env fingerprints и неизменного сохранённого результата.
Новый файл инвалидирует даже прежнее «не найдено».

Git status/diff/index, время, сеть, package install, неизвестные side effects,
edit/commit/publish/delete/send/process control не подавляются по старому PASS.
Вызывающий код может принудительно запросить fresh read для новой цели.
Проверки acceptance по умолчанию выполняются заново; отдельный
[`CheckCache`](../src/agent_harness/check_cache.py) требует явного allowlist,
полного proof. Cache v3 допускает только изолированные literal Python probes
через текущий interpreter с `-I -S`: constant print либо чтение относительного
UTF-8 файла и сравнение с константой. Произвольные tests, imports и opaque
commands не кэшируются. Перед записью и чтением proof заново проверяются source,
binary и environment; physical closure включает hidden/ignored inputs.
Неполная closure, links, special files и превышение bounds дают miss.
Final runner/gate остаются authority и выполняют обязательные проверки заново.

Дополнительно требуется доказанная startup/runtime closure. Текущая optional
policy поддерживает только отдельный Windows bundle с matching trusted Python
binary, конечным набором local runtime DLL, единственным relative ZIP в
versioned `._pth` и проверенными stdlib modules. Проверяются и bytes, и отсутствие
alternate `._pth`/`pyvenv.cfg`. Обычные global/venv/POSIX interpreters не дают
полного доказательства и не кэшируются. Ничего устанавливать для acceptance
не требуется: unsupported случай выполняется заново. Эта граница покрыта
реальным positive bundle и изменениями startup config, DLL и ZIP.

Для контролируемого `rg` reuse нужны явные `--no-config --no-ignore-parent
--no-ignore-global --no-ignore-vcs`, разрешённые search flags и относительные
пути внутри cwd. Govern не дописывает flags сам. Внешние paths, stdin,
`--hidden`, `--no-ignore`, `--follow` и неизвестные flags требуют fresh execution.

**Граница интеграции:** supervisor не перехватывает все встроенные tools Codex.
Govern является advisory для этих tools. Значение contract `ledger: govern`
не создаёт глобальный hook или OS sandbox. API govern может обслуживать явно
контролируемые вызовы; count suppressed не равен доказанной экономии токенов.

```text
python tools/harness.py work ledger show event-ID --run-dir /absolute/private/run --offset 0 --limit 4096
python tools/harness.py work ledger rebuild --run-dir /absolute/private/run
```

ID берётся из сохранённого event или inform packet. `show` возвращает bounded
UTF-8 fragment, content digest, total bytes и `next_offset`. Следующая страница
использует возвращённый offset; произвольный offset внутри UTF-8 символа
отклоняется. Возвращённый текст помечен как untrusted observation.

## Три уровня памяти

| Уровень | Содержимое | Проверка актуальности |
| --- | --- | --- |
| Short-term | Goal, constraints, checkpoint, active task, failures, files, next step, exact session и reported budgets | Current provenance и handoff reconciliation |
| Episodic | Problem signature, hypothesis/fix/root cause, observed outcome, symbols, receipt refs, trust и expiration | Source/toolchain плюс evidence bytes; default top-k=3, допустимо 1–3 |
| Semantic | Architecture/decisions/conventions в YAML и derived Markdown, relation graph | Claim source, revision/digest, actor, confidence, last checked |

Memory включается явно: `memory: true` в work contract. Без opt-in записи не
создаются через API `Memory`. Search локален: signatures/tags/terms и bounded
ranking; неподтверждённые идеи возвращаются отдельно как
`historical_hypothesis`. Никакого автоматического cross-repo import нет.
Semantic records создаются явным API; supervisor не выдумывает архитектурные
решения из любого successful run.

```text
python tools/harness.py work memory show episode-ID --store /absolute/private/state/memory --repository-identity REPOSITORY_SHA256 --limit 4096
python tools/harness.py work memory rebuild --store /absolute/private/state/memory --repository-identity REPOSITORY_SHA256
```

`REPOSITORY_SHA256` — сохранённая identity, не текущий commit SHA. Rebuild
восстанавливает index, relations и derived docs из authoritative YAML records.
Повреждённый authoritative record требует диагностики; он не становится
достоверным через пересборку index.

## Context Reducer и выбор tools

[`context_reducer.py`](../src/agent_harness/context_reducer.py) объединяет
дубликаты с first/last occurrence и count. Большой successful log заменяется
status/exit/fingerprint/evidence ID/pointer. Failure status и signature
сохраняются; stale snippets не становятся verified facts. Пропуски и
`next_offset` явны. Required contract/constraints при превышении бюджета
вызывают отказ с требованием chunking вместо скрытой обрезки.

Default budgets заданы отдельно: file snippet, error packet, RepoMap, memory,
handoff, agent context и tool result. Они измеряются UTF-8 bytes с резервом
Windows CRLF; bytes не равны model tokens. Raw evidence хранится отдельно.
Reducer не изменяет скрытую native history или compaction Codex.

Начинайте с узкого `rg`/direct file lookup. Для неизвестных связей используйте
ограниченный Rust RepoMap; для semantic definitions/references — on-demand LSP.
Четыре engineering query возвращают version-bound paths/lines/ranks и paging:

```text
python tools/harness.py repomap related-tests --root /absolute/repo --target src/auth.py --revision HEAD
python tools/harness.py repomap trace-to-code --root /absolute/repo --trace-file /absolute/private/trace.txt --revision HEAD
python tools/harness.py repomap change-impact --root /absolute/repo --changed src/auth.py --revision HEAD
python tools/harness.py repomap symbol-context --root /absolute/repo --symbol validate_token --path src/auth.py --revision HEAD --use-lsp
```

`--revision` должен обозначать текущий checkout; для иной revision используйте
её worktree. Results связываются также с actual tree digest. Ranking не
гарантирует полноту import graph; dynamic links могут отсутствовать. Trace
парсится как данные и не исполняется. Отсутствующий/stale LSP даёт явно
heuristic fallback. Stale line ranges не являются основанием для rename/edit.

TOON остаётся optional input representation. Условия измерения и fallback
описаны в [Performance Eval](PERFORMANCE_EVAL_RU.md). Пользовательский
`model_auto_compact_token_limit=500000` эти механизмы не уменьшают.
