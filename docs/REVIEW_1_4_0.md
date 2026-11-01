# Проверка 1.4.0: Rust, RepoMap и LSP

> Исторический отчёт указанной версии. Измерения и результаты ниже относятся
> к этому snapshot; актуальные команды — в [индексе документации](INDEX.md),
> текущая линия — [1.5.0](RELEASE_NOTES_1.5.0.md); результаты ниже не пересчитаны.

## Исполняемые изменения

Scanner/snapshot реализован в Rust, включая Git inventory, metadata, чтение и
SHA-256, canonical manifest/digest. В том же binary работает Tree-sitter RepoMap:
definitions, синтаксические references, взвешенный граф файлов и personalized
PageRank. Python сохраняет control plane, совместимый backend снимков и
on-demand stdio LSP client. БД, background daemon и второй model runtime отсутствуют.

LSP: symbols, definition, references, rename preview и применение плана с
before/after hashes. Сервер соответствующего языка запускается на время команды.
Встроены autodiscovery и поддержка стандартных Windows npm wrappers без shell.

## Дополнительные находки и исправления

| Проблема | Исправление |
|---|---|
| Сбой Git inventory мог выглядеть как пустой успешный snapshot | После распознавания Git root оба `ls-files` обязательны; ошибка отклоняет снимок в Python и Rust |
| Буквальная обратная косая в Unix Git filename превращалась в separator | Имена сохраняются без подмены; два разных файла не схлопываются; RepoMap сообщает о неподдержанном пути |
| Конкурентный chmod после первой замены терялся при compensation rename | Adoption/recovery сверяют mode, uid/gid, nlink, size/mtime и состояние ACL/xattrs; чужая правка сохраняется с recovery receipt |
| Broken bundled symlink выглядел как отсутствие native backend | Проверяется наличие самого directory entry; повреждённая установка не маскируется auto fallback |
| Metadata съедала последнюю полезную definition при бюджете 2 KiB | Сначала сокращаются необязательные сведения, сохраняются identity/counts и parser retained symbol |
| POSIX ожидание отдельного helper добавляло polling latency | Python waiter использует event; короткие Git команды не ждут фиксированные 2 ms |
| Pyright Windows URI с `%3A` вместо двоеточия диска отклонялся как относительный | URI декодируется ровно один раз, drive определяется после декодирования; confinement/UNC/traversal guards сохранены |
| Windows rename менял legacy DACL через наследование | Разделены legacy/modern setters; исходные ACE и control flags проверяются до записи staging и после замены |
| Неизвестный результат ACL мог выглядеть как завершённая компенсация | Неопределённость проверяется до сравнения bytes/stat; сохраняются `recovery_required` и backup |

Ранее исправленные running MCP status без `result.json`, FIFO/regular-file race,
CRLF output budget и metadata race сохранены. Rust на Windows использует настоящий
`FileBasicInfo.ChangeTime` и metadata открытого handle.

## Измерение default auto

Linux, CPython 3.12.14, Rust 1.90.0 release, готовый Linux binary из CI.
На fixture: один warmup каждого
backend и пять чередующихся пар. Один источник и контракт внутри каждой пары;
native process startup/JSON входят в замер, imports исключены, OS cache прогрет.
Полный цикл: init, freeze, три настоящие command checks, gate. В каждом цикле
checks passed, gate accepted, snapshot digests совпали.

| Fixture | Python, median | Default auto, median | Парное изменение wall |
|---|---:|---:|---:|
| 100 файлов × 1100 B, полный цикл | 0.464 s | 0.442 s | −2.25%; диапазон −4.81…+3.15% — различие неотделимо от шума |
| 3000 файлов × 16384 B, snapshot | 0.115 s | 0.058 s | −46.78%; все пять пар быстрее |
| 3000 файлов × 16384 B, полный цикл | 1.737 s | 1.089 s | −38.01%; диапазон −49.71…−28.91% |

Forced native на маленьком fixture оказался дороже Python: стоимость отдельного
процесса сохраняется даже после исправления ожиданий. Поэтому auto использует
Python ниже 512 наблюдённых файлов, Rust для большого/неопределённого дерева.
Hint — только выбор реализации в текущем процессе; исходники всегда читаются и
хешируются заново. `AGENT_HARNESS_BACKEND=native` доступен для прямого выбора.
Порог — измеренная стартовая эвристика, не утверждение о любом железе/файловой системе.

Воспроизведение: `tools/profile_native.py --candidate-backend auto` с указанными
files/bytes/pairs и новым output path. Raw JSON и source hashes включены в
release-архив. Проценты относятся к локальному deterministic cycle, не к GPT
generation, цене, реальным model tokens или качеству решения задач.

## Карта и реальный semantic сценарий

Fixture RepoMap: 53 Python files / 202364 bytes; карта 8155 bytes. По запросу
первым найден `validate_invoice`, без запроса центральный `normalize_amount`;
есть связанный файл и reference line. Late focus работает при max_files=1,
omissions/partial обозначены. Изменение body при прежнем Git HEAD меняет binding.

На самом harness карта `snapshot fingerprint` уложилась в 8062 bytes и нашла
Rust `snapshot`, Python `snapshot`, `verify_snapshot` и связанные проверки.
Пропущенные неподдержанные языки/paths не объявляются проанализированными.
Это уменьшение объёма навигации, а не сжатие всего содержимого без потерь.

Настоящий Pyright 1.1.414: cross-file references (три места), rename двух файлов,
применение, затем definition по новому имени. Child/server завершается после
команды. Перед apply сверяются bytes/identity всех файлов, после apply нужна
содержательная проверка проекта. Полная готовность индекса LSP не заявляется.

## Проверки и поставка

- Финальный общий локальный regression: 346 tests, 341 passed / 5 явно отмеченных
  skips. Реальный Pyright проверяется дополнительно отдельным обязательным smoke.
- Финальные compiled Rust tests: 19 passed. `tools/check_native.py` требует
  настоящий binary и проверяет exact manifests, Unicode/Git/submodule, distinct
  Unix names, real command/gate, stale rejection и bounded map с CRLF.
- `tools/check_lsp.py` требует реальный Pyright; отсутствие сервера считается
  failure, а не skipped pass. Локальный сквозной сценарий прошёл.
- Независимый reviewer воспроизвёл metadata race rename до/после исправления и
  проверил timeout/SIGINT cleanup изменённого native waiter: child и waiter завершаются.
- Повторный независимый review Windows fixes проверил URI confinement и обнаружил
  неверную compensation при неизвестном ACL; после исправления portable repro
  требует `recovery_required` и сохранённый backup. Win32 roundtrip подтверждает CI.
- CI: Windows/Ubuntu × Python 3.11/3.13, отдельные native build/test и real Pyright
  jobs. Итог конкретного commit и job logs фиксируются в release verification.
- Native bundles содержат SHA-256/build info, лицензии Cargo dependencies и Rust
  standard library. В Git хранятся исходники; готовые binary идут в архиве/CI artifacts.

Windows-specific paths считаются проверенными только по фактическому Windows CI
конкретного commit. macOS/ARM binaries в этом архиве не заявлены. Нет общей
атомарности snapshot файловой системы или межфайлового rename. Навигация не
заменяет gate; старые hashes не доказывают свежесть исходника.

Модели/provider/reasoning/security и auto-compaction 500000 сохранены.
