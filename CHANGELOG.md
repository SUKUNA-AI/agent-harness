# История изменений

## 1.4.1 — 2026-11-01

Исправления всех R01–R14 из повторного аудита. Подробности и границы проверки —
[отчёт 1.4.1](docs/REVIEW_1_4_1.md).

- Evidence: JSON boolean больше не равен number, включая вложенные значения;
  GoogleTest `notrun`/`suppressed` не удовлетворяет обязательный test case.
- Runner: attempt и workspace lease согласованы до запуска команды; gate
  повторно проверяет точное состояние под lock перед публикацией acceptance.
  Исчезновение ранее принятого optional receipt делает evidence устаревшим.
- Install/rollback: сохраняются Linux ACL/xattrs и поддерживаемые Windows DACL;
  вся транзакция проходит metadata preflight, обнаруженные чужие изменения
  сохраняются, неизвестное восстановление отражается в recovery state.
- LSP navigation повторяет запрос на открытых снимках целевых документов.
  Rename проверяет backup до компенсации и результат после неё; ограниченный
  ответ сохраняет статус, counts, digest и recovery receipt. Минимальный ответ
  проверяется до записи исходников.
- Чтение context packets и установочных файлов ограничено по размеру,
  использует nonblocking open и проверяет тип/идентичность открытого файла.
  На Windows второе bounded чтение выявляет изменение содержимого даже при
  задержке обновления file timestamps; scanner/snapshot не меняет свой путь I/O.
- Rust scanner: исправлена совместимость character classes с Python fnmatch;
  оба backend сохраняют пробельные символы в имени Git-root.
- Native build явно выбирает host target, связывает source fingerprint с
  собранным и скопированным бинарником, сохраняет прежний bundle при неудачном
  восстановлении. Реальная offline Cargo-проверка включена в native CI.
- Порог compaction 500000, model/provider/reasoning и permissions сохранены.
  Сервер, база данных и новые runtime dependencies не добавлены.

## 1.4.0 — 2026-11-01

Работающие локальные инструменты для больших репозиториев.

- Scanner/snapshot перенесён в Rust: inventory, metadata, SHA-256 и canonical
  manifest. Python backend сохранён для совместимости и прямого A/B; отсутствие
  native helper допускает fallback, найденная ошибка native — нет.
- `context map`: Tree-sitter definitions/references, граф файлов и PageRank с
  приоритетом задачи/выбранных файлов, output budget и content/revision binding.
  Граф живёт в памяти процесса; SQLite, daemon и embedding store отсутствуют.
- `code symbols/definition/references/rename/apply`: LSP по локальному stdio,
  cleanup child tree, UTF-16 positions, digest-bound rename preview и применение
  с проверкой всех входов/компенсацией ошибок.
- `native status/build`, фиксированный Cargo.lock, готовые native bundles и
  notices зависимостей. Обычная работа не требует compiler или скачивания.
- Убраны задержки POSIX subprocess polling; auto оставляет маленькие деревья
  на Python и использует Rust для больших, сохраняя полный rehash inputs.
- Исправлены silent-empty Git inventory/stage failures, слияние разных Unix
  имён с backslash, потеря concurrent permissions при rollback rename, неверный
  fallback повреждённого bundled symlink и пустая карта при слишком подробной
  metadata в бюджете 2 KiB.
- Windows: корректно разбираются encoded drive URI Pyright, сохраняются legacy
  и modern DACL; неизвестный исход восстановления прав не объявляется успешным
  откатом и оставляет recovery receipt с backup.
- CLI/workflows/skills используют готовые возможности по задаче. Existing
  handoff, bounded excerpts, compaction 500000 и пользовательские модели сохранены.
- FIFO/nonblocking, CRLF output cap, running MCP status и snapshot identity guards
  из 1.3 сохранены; native compatibility и command/gate проверяются отдельно.

## 1.3.0 — 2026-11-01

Дополнительное ревью, ограниченные tool outputs и продолжение долгих задач.
Архитектура и границы — [контекст и native core](docs/CONTEXT_AND_NATIVE_RU.md).

- Зарезервированное имя `tool_versions.executable` больше не может затереть
  fingerprint проверяемого executable; validation и runtime отклоняют коллизию.
- Regexp/report validation имеет отдельный timeout и изолированный worker.
  Строго literal assertions без reports остаются в быстром ограниченном пути.
  Таймаут сохраняет неуспешный receipt и закрывает ledger; raw evidence остаются.
- Выполняющаяся проверка без готового `result.json` видна через MCP metadata.
  Неполные/повреждённые evidence не выдаются за passed.
- Добавлены `status --summary`, read-only `handoff` и `context read` с полным
  file hash, проверкой изменения и ограничением реального размера JSON.
- Checkpoint проверяется на integrity и binding. Notes предыдущей попытки
  не переносятся в текущие инструкции. Freshness по умолчанию неизвестна.
- CLI загружает runner только на нужных маршрутах. Snapshot сокращает повторные
  metadata queries и отклоняет изменение объекта во время чтения.
- Nonblocking regular-file reads закрывают зависание при подмене файла на FIFO;
  UTF-8 pagination и Windows CRLF входят в проверки output budget.
- Optional `tool_output_token_limit=4096` описан отдельным примером; автоматически
  не применяется. Compaction 500000, модели, reasoning и безопасность сохранены.

## 1.2.0 — 2026-11-01

[Отчёт 1.2.0](docs/REVIEW_1_2_0.md): исправления F01–F11, OS-lock recovery,
обязательные JUnit cases, shared-record IDs, сохранение чужих edits при rollback,
точные executable fingerprints, зависимые задачи и честная свежесть evidence.
Добавлены source packets, review/handoff/wiki/experiment templates и browser
workflow. Локальное ускорение достигнуто сокращением повторных Git/probe/hash
операций; provider/model/effort и порог 500000 сохранены.

## 1.1.1 — 2026-11-01

Исправления по аудиту исходного `main` и сокращение повторяющихся инструкций.
Подробности проверки — [отчёт 1.1.1](docs/REVIEW_2026-11-01.md).

- Старый JUnit/JSON report не подтверждает новый запуск; обновлённый отчёт с теми
  же байтами допустим. Ошибки parsing/copy/snapshot сохраняют failed evidence и
  завершают запись процесса.
- `resume` проверяет принятое evidence и всю цепочку зависимостей общим валидатором.
  Новая attempt не увеличивает max_attempts и не отменяет/продлевает deadline.
- Native capacity учитывает явный `root_counts`: лимит только потомков Codex
  поддержан вместе с общим лимитом. Допустим один owner; checkout распознаётся
  по настоящим Git metadata, включая linked worktrees.
- Финальные manifest/active writes входят в установочную транзакцию. Сбой
  компенсируется, orphan targets не дают ложный unchanged/verified.
- Откат TOML проверяет семантику каждого изменения, включая agents keys:
  multiline-инструкции и посторонние настройки не меняются. Legacy cap alias
  переносится в один canonical key с обратимыми metadata.
- `doctor` добавляет read-only инвентаризацию model/provider/effort/context/tool
  output settings с происхождением; содержимое инструкций и credentials не входит
  в этот отчёт. Effective runtime не выводится из наличия файла.
- Bootstrap и процедуры сокращены; самостоятельная задача допускает прямой путь,
  независимые чтения объединяются, неизменные правила и успешные проверки не
  повторяются без причины. Модели, уровни effort и безопасность сохраняются.
- Уточнён приоритет role TOML над spawn effort и модельных defaults потомков.
  Устранены гонка в cancellation fixture и зависимость legacy fixture от LF/CRLF.
- Дополнительный review закрыл откат dotted config без конечного newline,
  включая старые manifests, и ложное принятие изменённого owned файла,
  совпавшего с новой версией package. Native API требуют явной семантики root cap.

- Установщик добавляет корневой порог автоматического сжатия истории Codex
  `model_auto_compact_token_limit = 500000`, сохраняя явное пользовательское
  значение. Поддержаны повторная установка, безопасный откат и прежние manifests;
  окно модели, provider, reasoning и безопасность не меняются.

## 1.1.0 — 2026-10-31

Первый оформленный [GitHub Release](https://github.com/SUKUNA-AI/agent-harness/releases/tag/v1.1.0)
переносимого общего комплекта для локального Codex.

- Глобальное подключение общих правил к новым чатам разных проектов после
  однократной установки в профиль; ручной вызов harness не нужен.
- Полезное распределение содержательных задач между штатными агентами,
  три роли, проверка пересечений файлов и зависимостей, полные брифы и repair.
- Пять устанавливаемых скиллов, включая создание скиллов, экономию контекста,
  интеграции и координацию; процедуры проверки и файлового checkpoint.
- Адресный установщик с dry-run, сохранением пользовательских настроек,
  резервными копиями и откатом; полный source ZIP для другого устройства.
- Отдельные необязательные plugin/MCP-примеры и обзор open source решений.
  БД, Claude-Mem, импорт чатов и фоновые службы не входят в установку.
- [Инструкция переноса](SETUP_RU.md), [границы автозагрузки](docs/AUTOLOAD_RU.md)
  и [фактически выполненные проверки](docs/RELEASE_REVIEW.md).

GitHub CI не стартовал из-за биллинга аккаунта; локальная проверка и hosted CI
остаются разными статусами. Native-проверки выполнялись в Windows; полная
native-квалификация Linux/macOS не заявляется.

## 1.0.2 — исходный снимок, 2026-10-31

[Коммит 6b260578](https://github.com/SUKUNA-AI/agent-harness/commit/6b260578649fadd6ecf4150573810d99bd63cfb6)
`Snapshot reviewed portable harness 1.0.2` — начальный снимок переносимого
репозитория. Его сообщение описывает состояние на момент того коммита.
Позднейшие изменения подняли версию до 1.1.0; старые сообщения Git не
переименовываются. GitHub может показывать старое сообщение рядом с файлами,
которые с тех пор не менялись. Версия source checkout указана в пакете и начале
этого changelog; опубликованные releases определяются своими тегами.
