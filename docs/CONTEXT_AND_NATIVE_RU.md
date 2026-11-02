# Контекст, snapshots и native backend — 1.5.1

Эта страница помогает выбрать минимальный инструмент для чтения проекта и
понять его стоимость. Полные flags/defaults/exit codes —
[CLI reference](CLI_REFERENCE_RU.md), состояние длительной задачи —
[эксплуатация](OPERATIONS.md), semantic navigation — [LSP](LSP_RU.md).

## Что выбрать

| Задача | Инструмент | Что означает результат |
| --- | --- | --- |
| Найти файл, строку, ошибку | `rg` / `rg --files` | Точное текстовое совпадение; самый короткий путь для известного фрагмента |
| Прочитать участок большого файла | `context read` | Ограниченный UTF-8 excerpt с SHA всего прочитанного файла |
| Передать несколько явно выбранных файлов | `context pack`, затем `context verify` | Content-bound packet; complete относится только к выбранным файлам |
| Выбрать важные файлы/определения по задаче | `context map` | Task-ranked **синтаксическая эвристика**, partial/limits отражены явно |
| Найти tests/impact/trace/symbol context | `repomap related-tests/trace-to-code/change-impact/symbol-context` | Bounded engineering query, version-bound paths и paging; heuristic fallback указан явно |
| Разрешить definition/references или rename | `code ...` | Ответ локального language server, зависящий от его semantic analysis |
| Проверить candidate для приёмки | `freeze` / `status --verify-evidence` / `gate` | Полный snapshot контракта и отдельная проверка evidence |
| Восстановить решения и следующий шаг | `handoff` | Metadata/checkpoint view; по умолчанию без workspace scan |

RepoMap помогает выбрать, что читать. LSP разрешает символ с учётом языка,
типов и workspace. Совпадение имени в RepoMap не доказывает semantic reference;
ни map, ни source packet, ни excerpt не подменяют acceptance snapshot.

В `work` reducer ограничивает model packet, а Ledger и opt-in memory возвращают
только актуальные наблюдения. Это отдельный слой над excerpts и RepoMap;
он не изменяет скрытую историю Codex. Optional cache имеет собственную строгую
physical/runtime closure и не переиспользует acceptance checks. Snapshot scope,
cache closure и полнота query inventory — разные гарантии. Подробности:
[Memory/context](MEMORY_AND_CONTEXT_RU.md).

## Адресное чтение и продолжение

```text
agent-harness context read --root . --file src/service.py --start-line 40 --lines 80 --max-bytes 8192
agent-harness context read --root . --file src/service.py --offset 4096 --expect-sha256 <actual-sha256> --max-bytes 8192
```

Во второй команде подставьте **возвращённый** `next_offset` и `sha256`; число
4096 — иллюстрация, не произвольное безопасное смещение. Offset начинается с 0
и должен находиться на UTF-8 boundary. Строки начинаются с 1. Очень длинная строка
может продолжаться в следующем ответе без разрезания Unicode символа.
`truncated` обозначает усечение запрошенного участка; `next_offset` может быть
ненулевым и после полного участка, если в файле остаётся следующий текст.

Default output — 8192 bytes; диапазон 1024–65536. Считаются сериализованный JSON,
escaping, metadata и финальный LF/CRLF. Это **не tokens** и не лимит raw text.
Вход до 16 MiB локально читается и хешируется целиком. При недостаточном месте
даже для content/metadata запрос завершается ошибкой вместо пустого «успешного»
excerpt. `--expect-sha256` отклоняет continuation по изменившемуся файлу.

## Явные source packets

```text
agent-harness context pack --root . --include src/service.py --include tests/test_service.py --output ../private/service-packet.json
agent-harness context verify --root . --packet ../private/service-packet.json
```

Родительский private каталог должен существовать или быть разрешённым местом
для создания output; он находится вне source root. Pack сохраняется эксклюзивно:
существующий packet не перезаписывается. В packet находятся реальные content,
размеры, SHA, root binding, общий digest, omitted и limits. Создание файла не
отправляет его во внешний сервис; последующую передачу определяют полномочия задачи.

Default: 64 файла, 64 KiB на файл и 256 KiB **суммарных исходных bytes**. Hard
bounds: 256 файлов, 256 KiB на файл и 1 MiB source bytes. JSON input packet
ограничен 4 MiB. Превышение per-file/remaining source budget отражается в omitted;
слишком длинный явный список файлов отклоняется. `complete=false` нельзя терять
при передаче. Verify подтверждает целостность/root binding и актуальные hashes
включённых файлов; оно не доказывает полноту всего проекта и не создаёт gate.

Packet input читается как bounded regular file. FIFO, symlink/reparse entry и
наблюдённая подмена отклоняются; nonblocking open не ждёт writer FIFO. На Windows
дополнительный bounded проход по тому же fd сравнивает реальные bytes, потому
что timestamps могут обновиться с задержкой при открытом handle. Финальные
identity/type проверки выполняются после verification. Это кооперативная проверка
снимка, а не блокировка любого внешнего writer.

Общие context guards отклоняют symlink/reparse components, выход за root,
credential/generated paths, binary/non-UTF-8 content и обнаруженные secret patterns.
Ограниченный scanner не распознаёт все секреты: root отвечает за допустимость
выбранных данных. `.git`, `.ssh`, `.aws`, node_modules, `.venv`, auth/credential
файлы и типовые private keys исключены из source выдачи.

## RepoMap

```text
agent-harness context map --root . --query "session refresh token" --max-bytes 8192
agent-harness context map --root . --focus-file src/session.ts --query refresh --max-files 4096
```

Rust helper извлекает definitions и identifier references через Tree-sitter,
строит weighted file graph и ранжирует его по запросу и focus files. Выдача
содержит symbols/signatures, диапазоны строк, SHA, соседние файлы и binding
наблюдённого scope. Dirty bytes меняют content digest при прежнем Git HEAD.
Parser errors, неподдерживаемый/ограниченный scope и `partial=true` нужно учитывать.
Content digest карты относится к наблюдённым файлам, не ко всем inputs контракта.

Поддерживаются Python, JS, TS/TSX, Rust, Go, C и C++. Учитываются Git-ignore,
повторяемые excludes, generated/credential paths и guards от symlinks. Focus и
совпадения query с путями повышают приоритет до ограничения scan. Если важный
символ не попал в бюджет, сузьте root/query или задайте focus; отсутствие в partial
map не доказывает отсутствие определения.

Defaults: 4096 файлов, 256 KiB/file, 64 MiB source total, 10 секунд, 8192 bytes
serialized output. Hard limits: 50000 файлов, 1 MiB/file, 128 MiB total, 30 секунд,
65536 bytes output; minimum output 2048. Полная таблица —
[CLI limits](CLI_REFERENCE_RU.md#source-context).

RepoMap требует native binary даже при `AGENT_HARNESS_BACKEND=python`: эта переменная
выбирает backend **snapshot**, а Python-реализации repomap нет. Если binary
недоступен, используйте явный `rg`/адресное чтение; результат не называется RepoMap.
Граф/parser state живут в памяти одного процесса. Нет daemon, SQLite, embeddings,
второго graph runtime или постоянного индекса.

## Native backend и маршрутизация

```text
agent-harness native status
agent-harness native build --offline
```

`status` показывает requested/selected/available/platform/path и проверенные поля
build-info для bundled binary. `build --offline` — явная локальная сборка, а не
автоматическая установка dependencies: нужны полный source checkout, Cargo,
C toolchain и уже заполненный Cargo cache. Без `--offline` Cargo может обращаться
к разрешённой сети. Сам snapshot не выполняет build/download.

| Режим | Snapshot policy |
| --- | --- |
| `AGENT_HARNESS_BACKEND=auto` (default) | Если binary отсутствует — Python. Если присутствует — adaptive routing |
| `...=python` | Python snapshot; существование optional bundled binary не требуется |
| `...=native` | Native обязателен; отсутствие/ошибка — явный отказ |
| `AGENT_HARNESS_NATIVE=<absolute executable>` | Явный developer-owned override; не означает проверенный packaged build-info |

Adaptive hint считает наблюдённые файлы до порога **512**: `<512` выбирает Python,
большой или неизвестный tree — native. Обход прекращается по лимиту 20 ms/64
каталога, использует excludes; Git ignored files могут завысить оценку и лишь
переключить backend. Process-local hint ограничен 128 entries, живёт до завершения
CLI process и **не содержит file hashes**. Малое дерево не получает scan «из cache».

Каждый snapshot заново обходит/хеширует фактические inputs. Оба backend возвращают
совместимый manifest/digest для raw bytes, dirty/untracked files, tracked deletion,
executable metadata, Git head/branch/settings и declared external inputs. На Git
root сохраняется значимый конечный пробел/CR/LF pathname: снимается только
разделитель вывода Git. Rust scope globs следуют Python fnmatchcase, включая bracket
classes и directory/**; произвольная собственная трактовка patterns недопустима.

Bundled binary проверяется по version/protocol/build-info и SHA. Memo неизменной
идентичности executable/metadata экономит повторное чтение **самого binary**, а
не проверку исходников. Present-but-invalid bundle, execution/protocol failure,
невалидный JSON и timeout не маскируются auto fallback. Python fallback разрешён
при отсутствии binary или явной Python routing policy; ошибка native не passed.
Native protocol v1 использует один JSON request на процесс, request до 16 MiB,
response до 100 MiB; process tree убирается после вызова.

### Сборка и восстановление bundle

Source builder использует `CARGO_TARGET_DIR` либо `native/target`, явно передаёт
`--target <rustc host>` и берёт binary из `<target-dir>/<host>/release`. Поэтому
`CARGO_BUILD_TARGET`/Cargo build.target не заставляют упаковать посторонний старый
файл из плоского `target/release`. Cargo может переиспользовать свои корректные
intermediate artifacts; это не обещание сборки с пустым cache. Source fingerprint
проверяется до/после; binary hash/build-info/license notices относятся к полученному
артефакту. Версии toolchain и source digest не подтверждают подпись автора.

Публикация подготовленного bundle сохраняет прежний destination для компенсации.
Если публикация и восстановление одновременно не удались, build возвращает
`recovery_required` с recovery_path и **не удаляет** уцелевший прежний bundle.
Сначала сверяйте содержимое указанного каталога; автоматического cleanup такого
recovery path нет. Исходники/настройки не нужно менять ради обхода этой проверки.

## Длинная сессия и стоимость

Полные raw logs, отчёты и observed receipts храните в разрешённом private evidence;
в историю передавайте результат, exit/status, нужную диагностику и reference/hash.
Brief дочернего owner содержит актуальные inputs/revision, точный cwd/scope,
criteria/output и конкретные reading sections. Долгая история root не является
обязательным входом каждой независимой проверки.

`status --summary` сокращает **ответ**, но сохраняет полный source snapshot.
`handoff` без verify читает metadata, не workspace: evidence_current=null и
source_freshness=not_checked сохраняются. Reader summary проверяет структуры JSON,
metadata/state/contract до 1 MiB, checkpoint до 4 MiB, result receipt до 256 KiB;
инспектируются не более 100 checks, остальные явно not_inspected. Группы показывают
omitted counts и references. Значение checkpoint current подтверждает binding
run/task/attempt/contract/candidate, а не текущие source bytes.

Checkpoint записывайте на существенной границе этапа: цель/уточнения, решения,
constraints, inputs/candidate, evidence, owners/operations и следующий безопасный
шаг. Он дополняет native compaction, не редактирует её opaque state и не запускает
вторую LLM summarization. Порог auto-compaction 500000 сохраняется как default
политики, явный пользовательский порог не подменяется. Это не размер context window
и не общий бюджет всех потраченных токенов.

Лимиты tools здесь измеряются в bytes, а client-side
[`tool_output_token_limit`](https://learn.chatgpt.com/docs/config-file/config-reference)
— отдельный параметр клиента. [Optional profile](../profiles/context-economy.example.toml)
не применяется установщиком автоматически. Процент уменьшения JSON нельзя переводить
в процент экономии тарифа: нужны реальные input/output/cached token measurements,
задержки и одинаковые задачи. [API compaction guide](https://developers.openai.com/api/docs/guides/compaction)
не доказывает доступность соответствующих hooks в конкретном UI.

Продолжение после закрытия приложения требует нового разрешённого запуска/сообщения
и фактической reconciliation. Файловый checkpoint не обещает native resume той же
сессии и не создаёт фоновую автоматизацию.

## Происхождение и проверяемые границы

Task-ranked map вдохновлён [Aider RepoMap](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/repomap.py),
semantic tools — [Serena/SolidLSP](LSP_RU.md#проверки-и-происхождение).
Точные pins/заимствованные процедуры — [provenance](../provenance/selected-patterns.json),
parser versions — [Cargo.lock](../native/Cargo.lock). Здесь собственный Rust scanner
и stdlib Python control plane, без чужого LLM runtime. Offline experiment templates
и процедурные идеи не равны проведённому behavioral evaluation модели.

Исходники: [context.py](../src/agent_harness/context.py),
[presentation.py](../src/agent_harness/presentation.py),
[native.py](../src/agent_harness/native.py), [repo_map.py](../src/agent_harness/repo_map.py),
[build_native.py](../tools/build_native.py). Проверки протокола, hashing и bounds —
core/native/context tests; реальная производительность, клиентская экономия токенов
и semantic completeness требуют отдельных наблюдений, а не только fixtures.
