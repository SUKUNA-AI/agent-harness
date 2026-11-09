# Границы доверия 1.5.1

User contract определяет разрешённые scopes, argv проверок и budgets. Model
plan, source, retrieved memory, trace, stdout и model response — недоверенные
данные. Они не могут повысить permissions, заменить required criteria, сменить
model/provider или дать право на публикацию.

## Применяемые ограничения

- Closed schemas, size/depth limits; tags, anchors, duplicate/unknown fields и
  будущие schema versions отклоняются. Парсинг не заменяет semantic validation.
- Проверенные argv/cwd вместо shell interpolation. Нет автоматических bypass,
  `--last`, force reset/stash или удаления пользовательских изменений.
- Writable phase требует явного contract scope; workspace-write выбирается для
  конкретного invocation, explicit installed read-only сохраняется. Reviewer
  ограничен read-only. Persistent security config/approvals не меняются.
- Один writer, OS-backed leases, PID+birth identity, durable global child
  registry. Потерянный checkpoint не доказывает отсутствие model process.
- Atomic private writes, symlink/reparse/FIFO refusal, bounded sanitized logs и
  immutable hashes. Raw logs не вставляются в prompt целиком.
- Cache default never; git status, unknown effects, writes, network/time/process
  dependencies не подавляются по старому success. Gate проверяет current source.
- Memory opt-in и repository-scoped, instruction-like text остаётся данными.

Optional cache v3 не доверяет заявлению `pure`/`complete`: fresh physical
inventory и runtime startup dependencies проверяются при put/get. Для Python
admission разрешён только narrow self-contained Windows bundle; ordinary
global/venv/POSIX runtime, imports, external reads и opaque commands дают miss.
Govern `rg` не переписывает argv; unsafe flags/outside scopes требуют fresh
execution. Required acceptance checks всегда выполняются заново.

Managed recovery проверяет worktree ownership до model resume. Неизвестная
creation identity, чужой сохранённый cwd или orphan child не освобождают slot.
При token budget неизвестный interrupted usage блокирует продолжение.

## Snapshot и физическое владение

Snapshot scheme v2 проверяет ancestors каждого inventory path, в том числе
tracked file под ignored parent. Default запрещает root alias и intermediate
symlink/junction/reparse; разрешённый leaf symlink представлен literal target,
а не bytes внешнего regular file. Python/POSIX reads используют nofollow dirfd
и повторную identity-проверку. Неподдержанная capability должна дать отказ.

`root_binding` хранит локальные device/inode, а полный `digest` связывает его
с manifest. `content_digest` исключает `root_binding`, но сохраняет cwd, Git
metadata, inventory policy и exclusions. Поэтому он не является автоматически
переносимым cross-worktree идентификатором; отдельные file SHA-256 характеризуют
bytes и не передают владение checkout или действительность evidence.
Смена snapshot scheme требует новой проверки; старые receipts не переписываются.

Installer проверяет весь target plan и сохраняет parent bindings. Его
write/replace/unlink привязаны к inspected directories; byte-identical target
после parent retarget не получает ownership по совпадению содержимого.
Компенсация проверяет metadata и сохраняет backups/journal при неполном restore.
Linux descriptor metadata path требует `/proc/self/fd`; Windows handles и ACL
нуждаются в отдельной реальной платформенной квалификации.

## Hooks и версии installation state

Управляемые `hooks.json` и launcher обратимы через общий installation protocol.
Идентичный payload устанавливается идемпотентно; unmanaged/duplicate harness
handler отклоняется без удаления чужих handlers. Входное событие передаётся
через stdin статическому launcher, а не интерполируется в shell code.
Installer сохраняет client trust state. `configured`, trusted handler,
фактический callback и поддержанный lifecycle проверяются отдельно; direct
Python smoke не является actual Codex qualification. Новый payload требует
штатного review и новой сессии, без обещания hot reload.

Новые install manifests используют schema v2; legacy v1 остаётся читаемым без
перезаписи цепочки. Старый normal active reader отвергает v2. Прямой вызов
старого бинарника с `--manifest` не получает ретроактивной защиты и не является
поддержанным downgrade. Пользователь сохраняет старые пакеты и backups для
разбора recovery, не ослабляя version checks ради повторного apply.

## Reports как недоверенные данные

JUnit profile `junit-harness-v2` учитывает suite errors и проверяет согласованность
aggregate counts; неизвестные структуры, namespaces, DTD/entities и превышение
bounds не дают PASS. Required pattern требует успеха всех совпавших случаев.
`reports.equals` — единственный явно открытый map внутри закрытого report
контракта; duplicate keys, non-finite numbers и превышение bounds отклоняются.
Подробные limits и JSON comparison semantics — в [приёмке](ACCEPTANCE.md#reports-json-types-и-skipped-cases).

## Остаточные ограничения

Worktree и cooperating locks не являются OS sandbox против другого процесса
с теми же правами. Выполнение trusted contract argv может запускать code проекта;
scope/hash checks обнаруживают изменение candidate, но не создают отдельную
permission boundary для произвольного hostile test process. Используйте
доступные OS/runner permissions по задаче.

Sanitization снижает риск утечки известных credential-shaped значений, но не
доказывает отсутствие всех возможных секретов. Private raw evidence не входит
в source/release. Не индексируется компьютер целиком; cross-repo import требует
отдельного действия. Hidden Codex tools и internal native agent graph могут быть
не наблюдаемы; govern для них advisory. CLI token cap проверяется между turns.

После unknown outcome сначала сверяются процессы, sessions и source. Stop
запрашивает отмену принадлежащего run процесса; неизвестные effects не replay.
Неполный journal tail сохраняется с hash до явного repair; corrupt complete
record даёт отказ. Publisher/сообщения/deploy требуют действующего разрешения
и не выводятся из зелёного gate.
