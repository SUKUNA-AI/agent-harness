# Native bundles — 1.5.1

Полный релизный ZIP содержит два готовых bundle: `linux-x86_64/` и
`windows-x86_64/`. В Git хранится эта инструкция, а скомпилированные файлы
исключены. Поэтому GitHub «Download ZIP» не заменяет полный релизный архив.

| Файл внутри bundle | Назначение |
| --- | --- |
| `harness-native` или `harness-native.exe` | Rust scanner/snapshot и Tree-sitter RepoMap |
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

## Обновление и диагностика

После обновления source checkout старый ignored `bin/` может остаться на месте.
Для версии 1.5.1 пересоберите helper или используйте полный новый релизный ZIP.
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
