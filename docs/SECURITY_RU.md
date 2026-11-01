# Границы доверия 1.5.0

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
