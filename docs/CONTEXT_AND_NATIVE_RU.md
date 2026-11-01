# Длинная сессия, расходы контекста и граница нативного ядра

## Что действительно реализовано

| Механизм | Статус и граница |
|---|---|
| Контракт, candidate, checks, gate, ledger и checkpoint | Исполняемый код; доказательства привязаны к содержимому и попытке |
| Выбранные source packets | `context pack` / `context verify`, явные файлы и hashes, без индекса |
| Адресное чтение | `context read`, лимит сериализованного ответа, hash всего файла, продолжение по byte offset |
| Компактный статус | `status --summary`; проверяет те же исходники, сокращает только ответ |
| Передача продолжения | `handoff`; read-only checkpoint decisions/next + текущие metadata, проверка binding |
| CWC / long-running, Superpowers, Pi | Применимые процедуры и templates: независимая приёмка, компактный review packet, передача состояния |
| Playwright | Workflow браузерной проверки и требования к evidence; браузер и CLI не устанавливаются автоматически |
| mini-SWE-agent | Принцип наблюдаемого ограниченного цикла, без второго agent runtime |
| GEPA/gskill / autoresearch | Процедура offline-эксперимента и template; optimizer и model-eval campaign не запускались |
| Deep Agents | Использованы идеи управления контекстом; отдельный behavioral evaluation suite не подключён |
| RepoMap по механизму Aider | Исполняемый Rust/Tree-sitter map: definitions, синтаксические связи файлов, task ranking и output budget |
| LSP по механизму Serena/SolidLSP | `code symbols/definition/references/rename/apply`; локальный сервер на одну команду, без daemon |
| Rust scanner/snapshot | Нативный обход и хеширование; exact совместимость manifest/digest с Python |

Источник идей и pins — [реестр](../provenance/selected-patterns.json). Процедура
задаёт поведение агенту, но не доказывает его соблюдение нативной моделью. Tests
ядра проверяют детерминированную механику; для оценки модели нужны реальные задачи.

## Как уменьшать объём, который попадает в историю

1. **Найти, затем прочитать.** Используй путь/символ/ошибку через `rg`, затем
   конкретный участок. Для большого файла `context read` возвращает ограниченный
   ответ. `--expect-sha256` предотвращает продолжение по старому файлу.
2. **Сохранить полный факт, показать нужную часть.** Raw logs/reports остаются в
   разрешённом evidence; в историю идут статус, exit code, краткая диагностика,
   путь и hash. Excerpt не является входом acceptance gate вместо полного отчёта.
3. **Передавать задачу с точными входами.** Бриф дочернего агента содержит scope,
   relevant files/revision, критерии и output contract. Полная история не нужна
   каждой независимой проверке. Spawn только при оправданной отдельной работе.
4. **Читать короткое состояние.** Для восстановления используй `handoff`. Для
   проверки текущего результата — `status --summary --verify-evidence`. Полный
   state остаётся доступен; dropped/omitted counts не скрываются.
5. **Сохранять устойчивые решения отдельно.** Project wiki содержит решения,
   причины, sources и условия пересмотра. Handoff — текущий шаг. Logs — исходные
   наблюдения. Не дублируй три вида данных в одном огромном документе.

Эти изменения снижают байты ответа выбранных tools. Фактические input/output/
cached tokens, модельная задержка и расход лимита измеряются отдельно в клиенте.
Нельзя переводить процент уменьшения JSON непосредственно в процент экономии
тарифа. Одна дополнительная дорогая итерация может перекрыть экономию текста.

### Ограничения инструментов

`context read` возвращает до 8192 bytes по умолчанию, включая JSON escaping и
LF/CRLF; доступный диапазон 1024..65536 bytes. Входной UTF-8 файл ограничен 16 MiB.
Файл локально читается и хешируется целиком, даже если модели нужен небольшой
участок. Длинная строка может продолжаться по `next_offset` без потери UTF-8.
Credential/path guards ограничены и не являются полноценным secret scanner.

`status --summary` не является дешёвым metadata polling: source snapshot остаётся.
`handoff` по умолчанию читает сохранённые metadata и не проверяет workspace.
`evidence_current=null` / `not_checked` нужно сохранить как неизвестность.
Неполный список или сокращённая заметка содержат признак пропуска и ссылку на
полные данные. `resume` остаётся отдельным действием reconciliation.
`checkpoint.status=current` означает целостность и привязку к текущим run/task/
attempt/contract/candidate. Свежесть файлов этим словом не подтверждается.
`--verify-evidence` проверяет уже записанную приёмку; без accepted verdict значение
`evidence_current` не становится true, а новые checks/gate не запускаются.
Metadata reader ограничивает state/contract 1 MiB, checkpoint 4 MiB, receipt
256 KiB и дополнительно проверяет структуру JSON. Просматриваются до 100 receipts;
непросмотренные явно считаются `not_inspected`. При превышении лимита нужно
прочитать нужную запись отдельно; данные не объявляются подтверждёнными.

Для нативного клиента есть отдельный параметр
[`tool_output_token_limit`](https://learn.chatgpt.com/docs/config-file/config-reference)
— бюджет отдельного tool/function output, сохраняемого в истории. В
[optional profile](../profiles/context-economy.example.toml) предложено начальное
значение 4096 для проверки на реальных задачах. Установщик его автоматически не
применяет, действующие overrides сохраняются. Полные evidence должны оставаться
доступны вне обрезанной истории.

## Долгая задача в одном чате

На границе существенного этапа checkpoint фиксирует цель, последние уточнения,
решения, constraints, attempt/candidate, доказательства, открытые операции и
следующий безопасный шаг. Не требуется checkpoint после каждого tool call.
После compaction агент читает компактный handoff, проверяет binding и догружает
только нужные файлы. Изменившиеся inputs требуют новой проверки соответствующих
выводов; старый accepted не превращается в актуальный сам по себе.

Порог auto-compaction **500000 сохраняется**. Это порог активной истории, не
суммарный бюджет сессии и не расширение context window. Выше порог не означает
монотонно меньший расход: есть стоимость повторно передаваемой истории, сжатия,
ошибок восстановления и дополнительных поисковых ходов. Для сравнения нужны
одинаковые длинные задачи и реальная telemetry клиента.

Нативная compaction и файловый checkpoint дополняют друг друга. Этот пакет не
запускает вторую LLM-суммаризацию, не редактирует opaque compaction state и не
перехватывает каждый tool call клиента.
[Официальная документация compaction](https://developers.openai.com/api/docs/guides/compaction) описывает API-механику;
она не является подтверждением доступности любого API hook в установленном UI.
Продолжение после закрытия приложения/сессии не обещается без нового сообщения.

## RepoMap: встроенный локальный инструмент

```text
agent-harness context map --root <project> --query "session refresh token" --max-bytes 8192
agent-harness context map --root <project> --focus-file src/session.ts --query refresh --max-files 4096
```

Rust helper извлекает определения и ссылки на идентификаторы через Tree-sitter,
строит взвешенный граф файлов и ранжирует его с учётом запроса и выбранных файлов.
В ответ входят symbols/signatures, диапазоны строк, SHA-256, соседние файлы,
Git HEAD и content digest наблюдённого scope. Dirty changes меняют binding даже
при прежнем HEAD. Это синтаксические связи по именам; точное разрешение типов и
одноимённых символов выполняет LSP. Parser errors и неполный scope обозначаются.

Defaults: 4096 файлов, 256 KiB на файл, 64 MiB суммарно, 10 секунд и 8192 bytes
ответа с учётом JSON и Windows CRLF. Пределы можно изменить явно; hard caps:
50000 файлов, 1 MiB на файл, 128 MiB суммарно, 30 секунд и 65536 bytes вывода.
`--focus-file` и совпадения query с путями повышают приоритет перед ограничением
сканирования. Если нужный символ остался за лимитом, сузь scope/укажи focus:
`partial=true` не является полной картой проекта. Content digest относится
только к наблюдённым файлам и не заменяет acceptance snapshot всего контракта.

Поддерживаются Python, JavaScript, TypeScript/TSX, Rust, Go, C и C++.
Git-ignore и явные excludes учитываются; generated/credential paths и symlinks
не используются для выдачи исходника. Ограниченный secret scan не гарантирует
распознавание всех секретов. БД, embeddings и постоянного индекса нет: graph и
parser state живут в памяти одного процесса. Небольшая адресная задача по-прежнему
начинается с `rg`, без ненужного полного сканирования.

Механизм взят из [Aider RepoMap](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/repomap.py):
definitions/references, weighted file graph, task personalization и бюджет карты.
Здесь используется собственная Rust-реализация этих механизмов; `diskcache`,
SQLite и Python graph runtime не нужны. Точные parser versions закреплены в
`native/Cargo.lock`, происхождение — в [реестре](../provenance/selected-patterns.json).

## Семантические операции через LSP

```text
agent-harness code doctor --root <project>
agent-harness code symbols --root <project> --file src/service.py
agent-harness code references --root <project> --file src/service.py --line 12 --column 5
agent-harness code rename --root <project> --file src/service.py --line 12 --column 5 --new-name new_name --output <private-state>/rename.json
agent-harness code apply --root <project> --plan <private-state>/rename.json
```

Встроенный клиент запускает установленный локальный language server по stdio,
выполняет запрос, закрывает сессию и убирает дочерние процессы. Постоянная служба,
MCP server и БД этому пути не нужны. Python: Pyright/pylsp; Rust: rust-analyzer;
JS/TS: typescript-language-server; C/C++: clangd; Go: gopls. Сервера — обычные
локальные инструменты соответствующего языка, их установка не происходит
незаметно. `--server` принимает явный JSON argv без shell. Настройки конкретного
проекта, например compilation database clangd, остаются ответственностью его
локального toolchain. Подробности — [LSP](LSP_RU.md).

Preview rename привязан к bytes и identity всех затронутых файлов. Apply сверяет
весь план до записи, отвергает stale inputs, выход за root, неоднозначные edits
и неподдержанные resource operations. Межфайловая запись не является одним
атомарным filesystem commit: при ошибке применяется компенсация, а конфликт с
чужой правкой сохраняется для восстановления. Обрезанный stdout не заменяет
полный план; для крупного rename используйте `--output`.

У Serena приложение GPL-3.0-or-later, SolidLSP MIT: это подтверждает
[pinned overview](https://github.com/oraios/serena/blob/ede329aaf0c824bea75455a2926f01420d7aa620/LICENSE).
В этом пакете реализована нужная функциональность symbol/navigation/refactoring
по LSP; установка полного Serena runtime не требуется. Лицензия не используется
как причина откладывать работоспособные инструменты.

## Rust в критическом пути

`core.snapshot` автоматически выбирает backend по короткому осмотру дерева.
До 512 наблюдённых файлов используется Python: здесь стоимость отдельного
процесса превышает выигрыш. Большое дерево или неопределённый размер выбирает
найденный bundled Rust helper. Hint хранится только в памяти текущего CLI
процесса, не является inventory и не кэширует hashes/приёмку. В Rust
выполняются обход, Git inventory/settings, metadata, параллельное чтение и
SHA-256, формирование manifest/digest; RepoMap также работает в этом бинарнике.
Управляющий слой Python сохраняет contracts, checks/gate, OS process containment,
installer и handoff. Замена scanner не меняет схемы сохранённых evidence.

```text
agent-harness native status
agent-harness native build
```

Native-архив включает готовые бинарники и notices зависимостей. Для обычного
исполнения compiler не нужен. Source build требует Rust 1.90.0 и C toolchain
(Tree-sitter grammars); Cargo.lock фиксирует зависимости. Команда build явная:
обычный check/context map не устанавливает и не компилирует ничего в фоне.

`AGENT_HARNESS_BACKEND=auto` — default с указанным выбором размера; отсутствие
бинарника оставляет совместимый Python snapshot. `native status` показывает
`selected=adaptive` и фактическую политику выбора. `python` явно выбирает прежнюю реализацию; `native` требует Rust.
Найденный повреждённый/несовместимый helper и ошибки scanner не маскируются
успешным Python fallback. `AGENT_HARNESS_NATIVE` — явный абсолютный путь к
локальному бинарнику для разработки/проверки. Bundle имеет SHA-256 и build info;
временные Rust/source caches не входят в историю Codex или git.

`tools/check_native.py` проверяет compiled backend, exact manifests/digests,
Git/non-Git и реальные command/gate outcomes. `tools/profile_native.py` сравнивает
обе реализации попарно на одном input tree с warmup, фиксированным объёмом и
полным cycle init/freeze/3 checks/gate. Native subprocess startup и сериализация
входят в измерение. Это локальная производительность, не скорость генерации
модели и не измерение её токенов. Фактические результаты конкретного релиза
публикуются отдельно; наличие Rust не заменяет A/B.
