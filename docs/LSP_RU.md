# Локальная навигация по коду и переименование

`agent-harness code` запускает установленный language server на время одного
вызова. Доступны symbols, definition, references и переименование через
проверяемый план. Сервис, база данных и постоянный индекс harness не создаются.
Серверы не входят в Python-пакет и не устанавливаются автоматически.

## Команды

```sh
agent-harness code doctor --root . --file lib.py
agent-harness code symbols --root . --file lib.py
agent-harness code definition --root . --file main.py --line 3 --column 9
agent-harness code references --root . --file lib.py --line 1 --column 5
agent-harness code rename --root . --file lib.py --line 1 --column 5 \
  --new-name welcome --output ../rename-plan.json
agent-harness code apply --root . --plan ../rename-plan.json
```

`--file` — относительный POSIX-путь внутри выбранного root. `--line` и
`--column` начинаются с 1. Column считает Unicode code points, не байты и не
UTF-16 units: emoji занимает одну пользовательскую колонку. На проводе позиции
преобразуются в UTF-16 согласно LSP. Смещение внутрь surrogate pair, за пределы
строки или внутрь UTF-8 символа отклоняется. CRLF и UTF-8 BOM сохраняются.

References по умолчанию включает объявление. `--exclude-declaration` передаёт
серверу соответствующий параметр. Определения и ссылки вне root не читаются;
ответ содержит число `outside_workspace_omitted`.

`doctor` проверяет доступность исполняемых файлов и known npm package metadata,
не запускает серверы и не объявляет runtime capabilities проверенными.

## Серверы

| Исходники | Автоматический выбор из PATH |
| --- | --- |
| Python `.py`, `.pyi` | `pyright-langserver --stdio`, затем `pylsp` |
| Rust `.rs` | `rust-analyzer` |
| TypeScript / JavaScript | `typescript-language-server --stdio` |
| C / C++ | `clangd` |
| Go `.go` | `gopls` |

Для явного выбора передайте argv в JSON:

```sh
agent-harness code symbols --root . --file lib.py \
  --server '["pyright-langserver","--stdio"]' --timeout 30
```

Команда запускается без shell. Для стандартных npm `.cmd`/`.bat` wrappers
`pyright-langserver` и `typescript-language-server` harness находит соседний
локальный или global-prefix `node_modules`, читает ограниченный `package.json`,
проверяет `bin` внутри пакета и запускает `node` с JS-файлом напрямую. Неизвестные
shell wrappers требуют явного executable + script argv. Для другого языка
можно указать `--language` вместе с `--server`.

Сервер получает root/workspaceFolders и точное содержимое открываемого файла.
Настройки проекта продолжает читать сам сервер. Необязательный `--settings`
принимает JSON object для LSP `workspace/configuration` и
`workspace/didChangeConfiguration`; например, поддерживаемые выбранным сервером
параметры анализа Python. Это не меняет Codex config, модель или permissions.

Локальный сервер — доверенная программа с правами запустившего пользователя.
Ограничения путей harness не являются OS sandbox для самого сервера.

## Готовность и точность результата

После initialize клиент открывает документ, выполняет documentSymbol barrier
при наличии capability и ограниченно ожидает завершения сообщённых progress
операций и периода тишины. Это полезнее одного initialize, но универсального
сигнала «весь проект полностью проиндексирован» в этом клиенте нет.
`readiness.complete_index_verified` поэтому остаётся `false`.

Результат навигации приходит от language server, включает хэши прочитанных
целевых файлов и повторную проверку их неизменности. Покрытие зависит от сервера,
его версии, конфигурации, зависимостей и границ workspace. Harness не заменяет
недоступные semantic references текстовым поиском под тем же названием. Если
сервера или capability нет, команда завершается с actionable ошибкой и code 2.

## Preview и применение rename

Обычный `code rename` не изменяет исходники. При появлении edits для закрытых
файлов клиент сначала захватывает их содержимое, отправляет didOpen, затем
повторяет rename. Набор затронутых файлов должен стабилизироваться за три
дополнительных прохода. Все захваченные файлы снова проверяются перед выдачей
плана.

Полный план содержит root binding, SHA-256, идентичность каждого исходного
файла, byte ranges и replacement text, хэши ожидаемых результатов и общий
digest. Он не содержит полных копий исходников. Digest проверяет целостность,
но не является подписью или отдельным разрешением на запись.

`--output` сохраняет полный план **вне root**, только по явному запросу,
без перезаписи существующего файла. `apply --plan` принимает именно этот файл.
Если полный план помещается в stdout, он также присутствует в поле `plan`;
внешняя оболочка ответа не является форматом plan-файла.

Apply проверяет весь план и все входы до замены первого файла, берёт общий
workspace lock для других harness apply, готовит временные файлы, сохраняет
recovery receipt и повторяет проверку перед каждой заменой. POSIX замены
привязаны к открытым дескрипторам каталогов. Замена каждого файла атомарна на
поддерживающей это файловой системе; **вся группа файлов не является глобально
атомарной**. Lock не блокирует редакторы и сторонние процессы; между последней
проверкой и OS-вызовом остаётся предел файловой системы без compare-and-swap.

При ошибке уже применённые файлы восстанавливаются, только если всё ещё
совпадают с записанным результатом. Обнаруженные конкурентные правки сохраняются.
Возможные статусы:

| Статус | Значение |
| --- | --- |
| `applied` | Все файлы заменены; временные staging и backups удалены |
| `no_changes` | План не меняет содержимое файлов |
| `failed_compensated` | Операция не завершена; выполненные замены компенсированы |
| `recovery_required` | Не все файлы можно безопасно восстановить; сохранены backups и receipt |

Последние два статуса дают exit code 2. При `recovery_required` сначала
прочитайте `recovery_receipt`, текущие файлы и указанные backup paths. Не
перезаписывайте обнаруженные изменения пользователя. При неожиданном завершении
процесса до компенсации receipt и staging могут остаться для ручного разбора.
Автоматического фонового восстановления нет. Старый план после компенсации
может стать stale из-за изменившейся идентичности файлов; создайте новый preview.

## Метаданные и ограничения файлов

- Запрещены credential/generated paths по общей политике source context,
  symlink/reparse components, выход за root, бинарные и не-UTF-8 файлы.
- Rename работает только с существующими regular files с одной hard link.
  Create/delete/move resource operations, annotated edits, повторные документы
  и overlapping/ambiguous edits отклоняются.
- На Linux сохраняются mode, executable bits, uid и gid. Файлы с xattrs/ACL
  или специальными platform flags отклоняются до записи. Убирать эти метаданные
  ради команды не нужно: используйте редактор, который их сохраняет.
- На Windows staging получает DACL исходного файла до записи содержимого;
  публикация через `ReplaceFileW` не включает флаги игнорирования ошибок ACL.
  Legacy/modern inheritance обрабатываются отдельно; exact DACL readback нужен
  до записи staging и после замены. Неизвестный исход даёт `recovery_required`
  с backup, даже при неизменных bytes/stat. Native API сохраняет поддерживаемые
  attributes и streams. Изменение
  владельца, SACL и все особенности сторонних файловых систем не обещаются.
- Безопасное применение metadata в этой версии ограничено Linux и Windows.
  На других POSIX-платформах navigation доступна; для rename используется явный
  отказ до изменений вместо неподтверждённого сохранения ACL.

## Лимиты и завершение процесса

`--timeout` задаёт общий бюджет LSP-сессии: по умолчанию 20 секунд,
допустимо 0,1–120. Завершение добавляет короткую попытку shutdown/exit, затем
принудительное завершение дерева и ожидание процесса. POSIX использует отдельную
process group, Windows — private kill-on-close Job Object. Серверные requests
configuration/progress обрабатываются, а `workspace/applyEdit` отклоняется.

Stdout выдаёт компактный UTF-8 JSON. `--max-bytes` по умолчанию 8192,
диапазон 1024–262144; лимит включает JSON escaping и до двух байтов финального
CRLF. При усечении остаются `total_items`, `returned_items`, `truncated`.
Усечённый rename preview не выдаёт частичный объект под видом полного плана:
`plan_included=false`; сохранённый через `--output` план остаётся полным.

Другие лимиты: 8 MiB на исходный файл, 64 MiB на захваченные navigation inputs,
64 MiB суммарных before/after bytes rename, 256 файлов, 10000 edits/items,
16 MiB на RPC frame и plan-файл, 64 MiB на входящий RPC stream. Stderr постоянно
дренируется; в памяти остаётся до 64 KiB, в ответе — только byte count и признак
усечения. Это ограничения входа и буферов, не общий лимит RSS или OS sandbox.

## Проверки и происхождение

`tests/test_lsp.py` проверяет настоящий stdio протокол через локальный fixture
server: configuration requests, прогресс, Unicode, таймауты, blocked stdin,
stderr, cleanup дерева, JSON/CRLF budget, сохранение планов, stale inputs,
symlink race и компенсацию IO/concurrency ошибок. Windows-only проверка сравнивает
DACL после normal apply и compensated failure.

Опциональный integration test запускает установленный Pyright при явно заданном
`HARNESS_TEST_LSP_SERVER` (JSON argv): cross-file references, rename preview,
apply и definition после изменения. Без этой переменной тест помечен skipped,
а semantic runtime не считается проверенным.

Архитектура локальных symbol tools вдохновлена
[Serena / SolidLSP](https://github.com/oraios/serena/tree/ede329aaf0c824bea75455a2926f01420d7aa620).
Здесь самостоятельная stdlib-реализация LSP-клиента; исходники Serena и SolidLSP
не копировались и не включены в поставку. Устанавливаемые пользователем серверы
сохраняют собственные лицензии.

Первичные спецификации:
[LSP 3.17](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/),
[WorkspaceEdit](https://github.com/microsoft/language-server-protocol/blob/gh-pages/_specifications/lsp/3.17/types/workspaceEdit.md),
[ReplaceFileW](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-replacefilew).
