# Контекст, handoff и файловая wiki

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
Symlinks/junctions и известные credential/generated paths отклоняются. Проверка
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
