# Дополнительное ревью и контекст — 1.3.0

> Исторический отчёт указанной версии. Измерения и результаты ниже относятся
> к этому snapshot; актуальные команды — в [индексе документации](INDEX.md),
> новая поставка — [1.4.2](RELEASE_1_4_2.md).

База ревью — версия 1.2.0, commit
[`a17b60f`](https://github.com/SUKUNA-AI/agent-harness/commit/a17b60f8bc0819c36be4a4c4a93351f8f0242577),
tree `cac39168318ad7a8ce9462568e50db51b29ed528`. Работа разделена на независимую
проверку acceptance/runtime, измерение startup/snapshot и проверку контекста/MCP.
Изменения reviewers интегрирует один root; приватные fixtures/evidence не включены
в исходники. История 1.2.0 остаётся в [предыдущем отчёте](REVIEW_1_2_0.md).

## Подтверждённые ошибки

| Приоритет | Воспроизведённое поведение | Исправление |
|---|---|---|
| P1 | `tool_versions.executable` затирал hash/resolved fingerprint основной команды; после замены игнорируемого executable check и gate могли пройти | Имя зарезервировано в contract validation, schema и `_versions`; обычные aliases сохраняют формат |
| P2 | Catastrophic regex мог зависнуть в finalization после завершения child, вне его timeout; receipt и ledger оставались незаконченными | Отдельная ограниченная фаза validation; regexp/report parsing исполняется изолированно, timeout даёт failed evidence и закрытие записи процесса |
| P2 | MCP `run_status` целиком падал, пока у running check ещё не создан `result.json` | Missing result обрабатывается как записанная выполняющаяся стадия; учитывается race с завершением. Corrupt/completed-without-result остаётся ошибкой |
| P2 | Подмена regular file на FIFO между stat и open блокировала контекстное чтение; тот же класс риска существовал у hashing | Nonblocking open и проверка regular descriptor; Windows сохраняет допустимую семантику обычных файлов |

Независимая проверка новых инструментов дополнительно выявила превышение output
budget при Windows CRLF. Лимит теперь резервирует оба байта newline. UTF-8 и JSON
escaping входят в размер ответа; продолжение длинной строки не теряет байты.

Validation budget — `min(5s, check.timeout, remaining run deadline)`. `validation`
в receipt содержит отдельные status/mode/timeout/elapsed и identity worker, если
он был создан. Strict literal assertions без reports используют ограниченный
`str.count`; все metacharacters, escapes, anchors и отчёты идут в subprocess.
Без assertions/reports worker не запускается. Process ledger отражает живую
validation phase; timeout/cancel/failure сохраняют durable receipt.

Worker protocol: request до 256 KiB, совокупно читаемые logs/reports до 64 MiB,
response до 1 MiB. Превышение — неуспех, не урезанное доказательство. В JUnit
summary до 100 имён длиной до 1000 chars с явными omission flags; counts и
required-case matching вычисляются по полным данным, raw XML сохраняется.
Это timeout parsing phase, не общий hard deadline для snapshot/probes/report
copy/artifact hashing, и не ограничение RSS/OS sandbox. Новый regex/report path
платит за запуск одного изолированного Python interpreter; эта цена оправдана
устранением неограниченного зависания. При гибели родителя состояние worker
требует существующего reconciliation по сохранённой process identity.

Snapshot `_entry` использует начальный `lstat`, hash и финальный `lstat` вместо
повторных `exists/is_file/stat`. Сравниваются identity, mode, size и timestamps;
замена файла после hash с теми же size/mtime и изменение размера отклоняются.
Стабильный формат manifest сохраняется. Это обнаружение изменений, не обещание
абсолютного filesystem CAS против процесса с теми же правами.

## Экономия контекста и продолжение

Добавлены исполняемые команды `context read`, `status --summary` и `handoff`.
Полный status остаётся default. Handoff проверяет checkpoint binding и не вызывает
`resume`/gate; notes чужого run или предыдущей попытки не становятся текущими
инструкциями. Значение `checkpoint.status=current` относится к binding, а не к
свежести workspace. По умолчанию свежесть неизвестна.

Воспроизводимый искусственный пример содержит две реально выполненные локальные
проверки (passed/failed), одну not_run, синтетические 80 закрытых owners и 30
подтверждённых операций, а также один unresolved outcome. Результат на UTF-8/LF:

| Данные | Полный размер | Ограниченный ответ |
|---|---:|---:|
| Status → summary | 64718 B | 2214 B |
| Сохранённый checkpoint → handoff с текущим summary | 60910 B | 2843 B |
| Stdout неуспешной команды → адресный excerpt | 98486 B | 830 B |

Все 14 semantic assertions этого fixture прошли: сохранены counts, failed status,
unknown outcome, note/next, строка диагностики, полный hash исходного журнала и
read-only поведение. Это сравнение bytes конкретных outputs, не measured tokens,
расход подписки, модельная latency или показатель качества решения задач.

Для применения, точных лимитов и статуса upstream mechanisms см.
[контекст и native core](CONTEXT_AND_NATIVE_RU.md). Aider RepoMap/Serena не выдаются
за реализованные функции. GEPA/gskill остаётся offline-процедурой, не запущенным
оптимизатором. БД, daemon, новая LLM-суммаризация и исключённые eval frameworks не
добавлены. Постоянный bootstrap не увеличен; подробности остаются в skills/workflows.

## Решение по ядру

Python сохранён; lazy imports CLI и адресное сокращение повторной работы включены.
Изолированный эксперимент lazy imports с готовыми `.pyc` показал `--version`
46.71 → 33.57 ms и `status` 62.72 → 57.89 ms (10 чередующихся пар, warm OS cache).
Это измерение конкретного изменения, не обещание ускорения всей сессии.

Первичная диагностика large fixture 3000 × 16384 bytes показала median 2.176 s
для create/freeze/трёх checks/gate в уже импортированном Python-процессе; около
84% времени приходилось на 11 snapshots. Parent CPU включает native hashing и
filesystem syscalls. Rust здесь не собирался и не сравнивался: compiler отсутствует.
Следующий нативный кандидат — scanner, если A/B подтвердит выигрыш; полный порт
рассматривается отдельно как решение о поставке готового runtime и совместимости.

### Финальное сравнение 1.2.0 и 1.3.0

Одинаковый tracked source, Python executable и contract; разные run directories
вне source. На fixture: один warmup каждого варианта и пять пар с чередованием
AB/BA. Таймер после imports, warm OS cache, instrumented cycle create → freeze →
три literal checks → gate. Exact contract/candidate digests совпали без нормализации.

| Fixture | Wall median 1.2.0 | Wall median 1.3.0 | Вывод |
|---|---:|---:|---|
| 100 × 1100 B | 0.459214 s | 0.454860 s | Различие неотделимо от шума: парные изменения −4.996..+3.969% |
| 3000 × 16384 B | 2.084994 s | 1.774988 s | Все пять пар быстрее; median paired −14.882% |

Metadata calls на source entry: 5 → 3, включая новый fstat regular descriptor.
Сохранены 11 полных snapshots, 66 Git queries и логические source hash bytes на
цикл: 1210000 / 540672000. Это не physical disk I/O. Во всех 24 циклах checks
passed и gate accepted, stdout/artifact hashes проверены. Parent CPU на large:
1.831869 → 1.532193 s. Счётчики добавляют overhead, пять пар — небольшой локальный
эксперимент. Вывод не распространяется на LLM latency, Rust или стоимость новой
worker-фазы для сложных regex/reports. Hash binding и raw samples входят в архив
поставки; runtime modules после измерения не изменялись.

## Проверка интеграции

- Полный локальный suite: **311 tests**, 28.749 s, **OK**, 3 platform-specific
  skips. В том числе реальные subprocess races, regex/JUnit timeout, evidence
  failures, installer/rollback, CLI/MCP, UTF-8 и CRLF cases.
- Release check: **113 files / 6 skills**, passed; `git diff --check` passed.
- Независимая повторная проверка worker: 7 probes прошли, включая cancellation,
  callback failure, hostile import paths, caps и FIFO. Отдельная проверка excerpt
  восстановила 24 комбинации UTF-8/CRLF/control/BOM/empty/line boundaries.
- CI исполняет suite/release check и optional MCP smoke на Windows/Ubuntu ×
  Python 3.11/3.13. Результат относится к конкретному commit в
  [Actions](https://github.com/SUKUNA-AI/agent-harness/actions); receipt поставки
  фиксирует проверенный run и head SHA.

## Сохранённые настройки и границы

- Порог `model_auto_compact_token_limit=500000`, model/provider, reasoning и
  безопасность сохранены. Явные overrides пользователя имеют приоритет.
- `profiles/context-economy.example.toml` — optional пример, не автоматическая
  смена бюджета вывода нативного клиента.
- Native Codex, реальные модельные токены/стоимость, browser workflow и качество
  относительно Claude этим тестированием не измерены.
- Локальная политика не обещает live hot reload, автоматический PreCompact hook
  или продолжение вычислений после закрытия приложения.
- Metadata/preview инструменты не заменяют полный gate и не доказывают отсутствие
  ошибки в скрытой части журнала.
