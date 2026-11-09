# Native bundles — 1.6.0

Полный релизный ZIP содержит два готовых bundle: `linux-x86_64/` и
`windows-x86_64/`. В Git хранится эта инструкция, а скомпилированные файлы
исключены. Поэтому GitHub «Download ZIP» не заменяет полный релизный архив.

| Файл внутри bundle | Назначение |
| --- | --- |
| `harness-native` или `harness-native.exe` | Rust scanner/snapshot и Tree-sitter RepoMap; Linux opt-in LSP stream mode |
| `build-info.json` | Версия, протокол, host target, toolchain, source digest, SHA-256 и размер бинарника |
| `licenses/index.json` и notices | Закреплённые зависимости и их лицензии |

Готовому helper не нужны Cargo, Rust и C compiler. Python 3.11+ остаётся
необходим управляющему CLI. Обычные commands не скачивают и не собирают helper.

## Проверить текущую поставку

Из корня пакета:

```sh
python tools/harness.py native status
python tools/release_check.py
```

`native status` показывает requested/selected backend, platform и metadata
найденного bundle. `adaptive` соответствует `auto` при доступном helper.
Версия и protocol должны соответствовать Python package; hash бинарника
проверяется перед использованием. `release_check` дополнительно сверяет source
fingerprint и notices. Проверка несовместимого найденного bundle завершается
ошибкой; она не маскируется отсутствием helper.

## Собрать из исходников

Нужны Rust **1.90.0** и C toolchain для Tree-sitter grammars. Используйте обычный
терминал с настроенным compiler; на Windows — окружение выбранного Rust host
toolchain с подходящим C compiler. Сборка выполняется для текущего host:

```sh
python tools/build_native.py
# Эквивалентный маршрут через CLI:
python tools/harness.py native build
```

Это альтернативные команды, запускать обе подряд не требуется. `--offline`
использует уже заполненный Cargo cache; свежая машина без зависимостей не сможет
построить offline bundle. Дополнительные параметры build script:
`python tools/build_native.py --help`.

Сборщик явно задаёт host target, использует `CARGO_TARGET_DIR` либо
`native/target`, берёт artifact из каталога этого target и связывает его с
native source fingerprint до/после сборки. Новый bundle подготавливается в
staging-каталоге, затем заменяет прежний. Если компенсация не удалась, старая
копия сохраняется вместе с указанием recovery path.

Собрать Windows helper из Linux одной этой командой нельзя: она не является
cross-compilation pipeline. Поставляемые платформы собираются отдельными CI jobs.
Другие архитектуры/ОС не заявляются проверенными лишь потому, что имя platform
распознаётся кодом.

## Library и явный wire v2

Тот же Cargo package содержит library и executable. Обычный запуск executable
сохраняет v1 snapshot/repomap/info и их digest semantics. Новый закрытый envelope
выбирается только аргументами `--protocol-version 2`; сейчас его wire operation —
`capabilities` версии 1. Файловое ядро используется внутри snapshot/RepoMap,
отдельный filesystem/context/generic-process wire API не заявлен.

Linux capabilities также объявляют `stream_modes` с `lsp-session` protocol v1.
Это отдельный long-lived `--lsp-session <session-id>` entrypoint на Tokio, не
one-shot operation v2. Его использует Python LSP adapter при явном
`AGENT_HARNESS_LSP_BACKEND=native`; default остаётся Python. Session protocol,
границы Linux/glibc qualification и возврат к Python описаны в
[LSP reference](../docs/LSP_RU.md#лимиты-и-завершение-процесса).

Пример для собранного Linux bundle из корня пакета:

```sh
printf '%s\n' '{"protocol_version":2,"operation":"capabilities","operation_version":1,"request_id":"discovery-1","budget":{"timeout_ms":1000,"max_response_bytes":4096},"input":{}}' | bin/linux-x86_64/harness-native --protocol-version 2
```

V2 отклоняет duplicate keys, неизвестные поля закрытого envelope, неверные типы,
unsupported versions и превышение bounds. Request/response ограничены 65536
bytes, depth — 32, nodes — 8192; response budget 1024–65536 включает финальный LF.
Response связывает request ID и operation/version, сообщает package version,
OS и architecture. Exact binary SHA/source fingerprint проверяются отдельно
по `build-info.json`; эти сведения не являются аутентификацией автора.

Поле `protocol_version: 1` существующего build-info описывает legacy entrypoint;
его schema не переопределяется. Новые capabilities читаются явно у того же
проверенного бинарника. Snapshot routing `auto` и порог 512 файлов сохранены.
Linux [strict read/hash](../docs/CONTEXT_AND_NATIVE_RU.md#общее-файловое-ядро-rust)
не переключает Python файловые consumers.

При проверке поставки запускайте `tools/check_native.py` и
`tools/check_native_build.py`; первый требует настоящий helper, второй проверяет
сборку и сохранение предыдущего bundle при ошибке публикации. ZIP проверяется
через `tools/build_release.py`, включая extracted install/verify/rollback smoke.
Linux-only candidate не квалифицирует новый Windows/macOS код.

Rollback программы выполняется согласованной копией package и bundle с
проверенными hashes, после завершения активных операций. Не заменяйте один
executable посреди Session и не редактируйте metadata ради совпадения версии.
Эти read-only изменения не мигрируют durable state и не требуют изменения
профиля Codex. Rollback установленного профиля остаётся отдельной операцией
по [SETUP_RU](../SETUP_RU.md).

## Обновление и диагностика

После обновления source checkout старый ignored `bin/` может остаться на месте.
Пересоберите helper для версии исходников или используйте полный релизный ZIP.
Не меняйте только поле version или manifest старого бинарника.

| Состояние | Поведение |
| --- | --- |
| Helper отсутствует, backend `auto` | Snapshot использует совместимый Python backend |
| Helper отсутствует, команда `context map` или backend `native` | Явная ошибка о необходимом native helper |
| Helper присутствует, версия/hash/протокол неверны | Явная ошибка, без silent fallback |
| Helper завис/вернул неверный ответ | Timeout/protocol error, не успешный Python result |
| Указан `AGENT_HARNESS_NATIVE` | Явный абсолютный путь к developer-owned executable; bundled identity не приписывается внешнему бинарнику |

Полная схема `auto` и порог 512 файлов — в
[архитектуре](../docs/ARCHITECTURE_RU.md). Команды и бюджеты — в
[CLI reference](../docs/CLI_REFERENCE_RU.md). Локальные benchmarks прошлой версии
сохранены в [отчёте 1.4.0](../docs/REVIEW_1_4_0.md).
