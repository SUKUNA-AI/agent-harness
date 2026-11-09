# Native Agent Harness 1.6.0

[Релиз и вложения](https://github.com/SUKUNA-AI/agent-harness/releases/tag/v1.6.0) ·
[Установка и обновление](../SETUP_RU.md) · [Карта документации](DOCUMENTATION_MAP_RU.md)

Релиз опубликован 9 ноября 2026 года. Тег `v1.6.0` закрепляет source commit
[`94cf9769e588bb8ed664cf4fe81b4e5769997f57`](https://github.com/SUKUNA-AI/agent-harness/commit/94cf9769e588bb8ed664cf4fe81b4e5769997f57).
Документация в `main` может уточняться позднее; опубликованный тег и архив
сохраняют исходный состав поставки.

## Что изменилось

- **Native-сессия.** Установщик добавляет SessionStart handler и owned launcher.
  Короткий bootstrap вызывает Memory, recall, Ledger inform и reducer.
  Checkpoint, явное обновление цели и typed receipts связывают продолжение с
  текущим root и evidence. Открытый клиент требует отдельной проверки trust
  и invocation; изменённые файлы не означают hot reload.
- **Проверяемый план и исполнение.** Managed contract/plan v2 сохраняет исходную
  mission и executable checks. Новый managed run проходит plan-quality gate.
  Local и integrated node checks открывают зависимую работу только по текущим
  acceptance receipts. Coverage, fresh review и общий core gate проверяют
  итоговый candidate.
- **Восстановление.** Invocation/completion/integration receipts позволяют
  различать intent, подтверждённый dispatch и неизвестный outcome. Resume
  сохраняет исходные attempts, deadlines и usage; неопределённый writer
  сначала требует reconciliation.
- **Windows paths и IO.** Root binding использует физическую идентичность
  каталогов, полный FileId и проверку reparse ancestors. Git export сохраняет
  blob bytes при точном преобразовании LF в CRLF. Native packets поддерживают
  длинные пути и разные диски в прежнем byte budget.
- **Журнал.** Процессы согласуют захват журнала и проекций. Повторное использование
  head cache проверяет bytes журнала, а recovery сверяет маркер до публикации
  исправления. Кэш не отменяет проверку содержимого.
- **Нативное ядро.** Portable ZIP включает Rust snapshot и RepoMap для Linux и
  Windows x86_64. Wire v1 сохраняется; явный v2 сообщает capabilities.
  Linux LSP stream mode остаётся optional.

Выбранные model/provider/reasoning, permissions и явный compaction override
сохраняются. Базовому комплекту не нужны daemon, БД, отдельный LLM API или
дополнительный ключ.

## Версии контрактов

Номер продукта не является версией каждого формата.

| Объект | Контракт в 1.6.0 |
| --- | --- |
| Managed work contract и plan | v2; legacy v1 сохраняет прежнюю семантику |
| Новое managed work-state | v3 с plan-quality assessment |
| Execution receipt / final review | v2 / v3 для новых verified runs; legacy readers сохраняются |
| Snapshot projection | scheme v2 с physical root binding |
| Native observer / operation / bridge receipts | v2; legacy v1 не получает новую квалификацию |
| Helper wire | v1 по умолчанию; v2 только при явном выборе |

Адресная миграция work-state v1→v2 не заменяет планирование нового state v3.
Перед изменением сохранённого run смотрите
[форматы и миграцию](STATE_FORMATS_AND_MIGRATION_RU.md).

## Скачать и проверить

Используйте [`agent-harness-1.6.0.zip`](https://github.com/SUKUNA-AI/agent-harness/releases/download/v1.6.0/agent-harness-1.6.0.zip).
Автоматический GitHub Source code ZIP не включает готовые native bundles.

| Вложение | Назначение |
| --- | --- |
| `agent-harness-1.6.0.zip` | Полный portable package и оба native bundles |
| `release-manifest.json` | Source revision, modes, размеры и SHA-256 файлов |
| `VERIFICATION.json` | Фактические проверки, source-bound native artifacts и ограничения |
| `SHA256SUMS` | Контрольные суммы ZIP, manifest и verification report |

SHA-256 полного ZIP:

```text
b0f252fd152913c67bbc8de0c34dc8961a6945ff444e734bde0f6760ca9d6329  agent-harness-1.6.0.zip
```

## Фактические проверки и ограничения

- Linux/Windows native bundles взяты из успешных jobs
  [исходного CI](https://github.com/SUKUNA-AI/agent-harness/actions/runs/42571184286).
  Их version, native source digest, binary hash и licenses проверены при упаковке.
- Две сборки portable ZIP совпали по bytes. Проверены все записи manifest,
  извлечение и install/verify/reinstall/rollback в изолированном Linux-профиле.
  После загрузки release assets были скачаны и сравнены по SHA-256.
- [Публикация и финальная проверка](https://github.com/SUKUNA-AI/agent-harness/actions/runs/42589164845)
  завершились успешно; тег и четыре вложения прочитаны обратно.
- **Полный Python CI имеет известные таймауты сценариев исполнения и
  восстановления. Полный PASS не заявляется.** Snapshot статусов и ограничения
  находятся в приложенном `VERIFICATION.json`.
- Новая проверка `work start/resume` с реальной моделью для этого выпуска не
  выполнялась. Windows native jobs и Linux installer smoke — отдельные
  наблюдения; они не подтверждают поведение любого клиентского профиля.

## Обновление профиля

Распакуйте ZIP в постоянный каталог и сохраните прежний package/private state.
Выполните doctor → install dry-run → install `--apply` → verify → повторный
install с ожидаемым `unchanged`, используя прежние каталоги профиля и состояния.
Затем откройте новый процесс Codex, проверьте handler через `/hooks` и выполните
применимую native-проверку. GitHub merge или публикация ZIP сами профиль не
обновляют. Подробные POSIX/PowerShell команды и rollback —
в [SETUP_RU](../SETUP_RU.md).
