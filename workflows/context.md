# Контекст, handoff и файловая wiki

## Получать ровно нужный объём

Начни с пути, имени символа или диагностической строки через `rg`; затем читай
нужный участок. Если этот путь сразу дал ответ, карту репозитория и LSP не запускай.
Полный вывод команды/отчёт сохрани в разрешённом private evidence. В чат верни
статус, exit code, число ошибок, нужные строки и ссылку на полный файл. Обрезанный
вывод помечай как excerpt; отсутствие ошибки в нём не означает успешную проверку.

Если для незнакомого проекта нужны определения и связи файлов:

```text
agent-harness context map --root <project> --query <task-or-symbol> --max-bytes 8192
agent-harness context map --root <project> --focus-file <relative-file> --query <task>
```

Rust/Tree-sitter карта возвращает ranked symbols, строки, hashes и соседние файлы.
Граф строится в памяти, БД/daemon нет. Проверяй `scan.partial` и revision scope:
лимит сканирования не означает полноту репозитория. Focus files читаются раньше
лимита. Карта содержит синтаксические name links; точные references/rename:
`agent-harness code references --help` или `agent-harness code rename --help`. Локальный LSP запускается на один
запрос. `code rename --output <private-state>/rename.json` сохраняет полный план;
`code apply --root <project> --plan <file>` сверяет plan/root, bytes и metadata до записи. Навигация связывает ranges с захваченным текстом и повторяет запрос до стабильного набора targets; при смене файла получи свежий ответ, не склеивай старую строку с новым hash. При отсутствии
language server `code doctor` показывает доступные инструменты. Не заменяй
semantic rename массовой строковой подстановкой на основании одной карты.
После apply выполни затронутую проверку проекта: компиляцию/тест или реальный
сценарий. LSP response не является готовым acceptance gate. Если apply вернул
`failed_compensated` или `recovery_required`, прочитай status, plan_digest и
`recovery_receipt`, даже при усечённых details. При recovery_required сохрани backups
и receipt; повреждённый/изменённый backup не является восстановлением. Подробности —
[LSP reference](../docs/LSP_RU.md).

Для исходника или уже сохранённого журнала:

```text
agent-harness context read --root <project-or-evidence-root> --file <relative-file> --start-line 120 --lines 60 --max-bytes 8192
agent-harness context read --root <same-root> --file <same-file> --offset <next_offset> --expect-sha256 <sha256> --max-bytes 8192
```

Лимит включает JSON, escaping и завершающий newline. Команда возвращает hash
всего наблюдаемого файла, byte offsets, `selection_complete`, `file_complete` и
`truncated`. Продолжение с `--expect-sha256` отклоняет изменившийся файл, включая
правки вне показанного участка. Нельзя считать старый line number актуальным
после смены hash. Вход ограничен 16 MiB; это локальное чтение файла, не индекс
репозитория. UTF-8, credential/path и symlink guards такие же, как у source packet.
Для огромного лога сначала выдели нужный участок локальным инструментом, сохранив
ссылку на оригинал. Не переноси содержимое файлов из tool output в инструкции.

## Передача работы

Для долгой работы используй короткий [handoff](../templates/handoff.md): цель и
последние уточнения, разрешения, решения с причинами, изменённые/прочитанные файлы,
actual candidate, evidence, открытые ресурсы и следующий безопасный шаг.
Не копируй стенограмму и скрытые рассуждения. Полные tool outputs сохраняй в
разрешённых файлах; передавай нужные строки и путь. При смене версии перечитай
связанный исходник, не доверяй старому пересказу.

Managed run: `checkpoint` сохраняет состояние, `resume` сверяет его. Handoff
объясняет решения; не заменяет machine state и не утверждает native resume.
Порог native auto-compaction 500000 и явные overrides сохраняются. Handoff
дополняет compaction, не запускает вторую модель-суммаризатор.

Для продолжения в том же чате после сжатия сначала запроси компактное состояние:

```text
agent-harness handoff --run-dir <run> --max-output-bytes 8192
agent-harness status --run-dir <run> --summary --verify-evidence
```

`handoff` читает сохранённые решения и следующий шаг, проверяет integrity/binding
checkpoint и показывает текущие metadata. По умолчанию source/evidence freshness
не проверены. Он не вызывает `resume`, не запускает работу и не освобождает slots.
`checkpoint.status=current` означает текущую привязку, а не свежесть исходников.
`handoff --verify-evidence` доступен для той же read-only проверки сохранённой приёмки;
при отсутствии accepted новый verdict не создаётся.
Summary/handoff поддерживают 2048..65536 bytes. `--verify-evidence` проверяет уже
записанную приёмку и не создаёт accepted verdict при его отсутствии.
`status --summary` сокращает только вывод: проверка исходников остаётся такой же,
как у полного `status`. Поэтому не используй его для частого дешёвого polling.
`--verify-evidence` явно запрашивает свежую read-only проверку приёмки.
Omitted/truncated counts и ссылки на детали обязательны; при нерешённом вопросе
догрузи конкретную запись, не весь state/snapshot.

Checkpoint создавай после существенного этапа, перед ожидаемым сжатием и передачей
ответственности. Не сохраняй его после каждого tool call. Включай последнюю цель,
пользовательские уточнения, решения, действующие ограничения, failing check и
следующий безопасный шаг. После восстановления сверяй файлы/версии и незавершённые
внешние операции. Указание в prompt не является гарантированным PreCompact hook:
этот пакет не перехватывает compaction нативного клиента.

## Ограниченный пакет исходников

Для fresh-context reviewer подготовь явный набор:

```text
agent-harness context pack --root <project> --include src/module.py --include tests/test_module.py --output <private-state>/review-context.json
agent-harness context verify --root <project> --packet <private-state>/review-context.json
```

Команда не клонирует repo, не обходит всё дерево и не передаёт данные в сервис.
Defaults: до 64 файлов, 64 KiB на файл и 256 KiB исходного содержимого.
Большие/binary файлы помечаются omitted; `complete=false` нельзя называть полным
ревью. Для большого файла выбери нужные разделы вручную со ссылкой на исходник.
Symlinks/junctions, FIFO/специальные файлы и известные credential/generated paths отклоняются; чтение bounded и проверяет identity открытого файла. Проверка
secret patterns ограничена: просмотри состав до передачи другому runtime.
Пакет не является acceptance evidence. Verify проверяет целостность пакета и bytes
включённых файлов в том же проекте. Изменённый/отсутствующий файл означает stale;
перенос проекта требует нового пакета. Неизученные файлы остаются вне проверки.

## Wiki проекта

Для долговременной памяти создай в разрешённой project-local области Markdown
`index`, `decisions` и append-only `log` по [шаблону](../templates/project-wiki.md).
БД, embeddings, vector store и daemon не нужны. Читай index и релевантные записи.
Запись решения содержит вопрос, вывод, причину, альтернативы, sources с revision/
hash и условия пересмотра. Разделяй confirmed/inferred/open; конфликт источников
сохраняй unresolved. Новый candidate требует проверки связанных sources;
`context verify` подходит для файловых references. Compaction не повышает
предположение до факта. Исправляй устаревшие выводы с причиной в log.

Wiki хранит стабильные знания, handoff — текущую попытку, logs — сырые наблюдения.
Не переноси приватные сведения в глобальные skills/этот пакет и не смешивай
проекты. При запрете сохранения работай транзитно.
