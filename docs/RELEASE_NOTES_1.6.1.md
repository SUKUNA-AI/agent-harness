# Native Agent Harness 1.6.1

[Релиз и вложения](https://github.com/SUKUNA-AI/agent-harness/releases/tag/v1.6.1) ·
[README](../README.md) · [Установка и обновление](../SETUP_RU.md)

Патч-релиз объединяет исправления, внесённые в `main` после 1.6.0, и обновляет
README: один путь установки, основные команды, настройки и границы возможностей.
Старые GitHub Releases, теги и их архивы сохраняются.

## Что изменилось после 1.6.0

- **Managed agents:** до 12 одновременных model sessions на общий state root,
  включая planner, reviewer и integration owner. Не более трёх reviewer
  одновременно; остальные роли могут занимать свободные slots. Меньший cap
  конкретного контракта сохраняется.
- **Effort по роли и риску:** owner/architect/diagnostician используют
  `medium` или `high`, reviewer — `high` или `ultra`. Высокий риск выбирает
  большее значение; допустим явный совместимый per-node override. Planner
  использует `high`. Это настройки отдельных `codex exec/resume`, а не
  изменение модели/provider или effort координатора.
- **Rust:** snapshot `auto` предпочитает проверенный helper для любого размера
  дерева. LSP `auto` использует Rust transport на Linux/Windows. Windows
  containment сохраняет suspended launch → private Job → resume. Ошибка
  выбранного helper не приводит к повторному исполнению через Python.
- **SessionStart:** Memory, recall, Ledger inform и reducer используют
  ограниченный `bounded_context` по cwd и instruction-файлам предков.
  Bootstrap не сканирует всё домашнее дерево. Неполный source inventory,
  omissions и source-unverified checkpoints обозначаются явно.
- **Повторное чтение и восстановление:** исправлены Windows local-slot
  recovery, own-only inventory, обработка неполного checkpoint proof и
  идемпотентность ограниченного session replay, включая broken pipe.
  Неизвестный outcome по-прежнему требует reconciliation.
- **Установка:** POSIX install/verify допускает эквивалентное имя Python в том
  же лексическом каталоге только для точного owned hook template и одного
  файла интерпретатора. Разные virtualenv/каталоги не объединяются; Windows
  сохраняет точное написание команды.
- **Recovery budgets:** исходные deadlines и attempts сохраняются; restart
  не выдаёт новый бюджет. Повторная валидация уже захваченных YAML/schema inputs
  устранена без ослабления проверки актуальности candidate.
- **Документация:** README упрощён, отделены native child cap от managed slots,
  указаны Linux/PowerShell установка, trust hook, обновление и диагностика.

`agents.max_concurrent_threads_per_session` — отдельный лимит native children
Codex, без root. Portable default остаётся **5**; для намеренного выбора **12**
используйте `install --max-child-threads 12` в dry-run и apply. Managed cap 12
не изменяет этот параметр автоматически. Native-роли TOML по-прежнему задают
owner/reviewer `high`, helper `medium`.

## Поставка и проверка

Полный `agent-harness-1.6.1.zip` содержит переносимые исходники, инструкции и
native bundles Linux x86_64/glibc и Windows x86_64. Автоматический GitHub
«Source code ZIP» готовых binaries не содержит.

| Вложение | Назначение |
| --- | --- |
| `agent-harness-1.6.1.zip` | Полный пакет с Linux/Windows native bundles |
| `release-manifest.json` | Source revision, размеры, modes и SHA-256 файлов |
| `VERIFICATION.json` | Проверки этого candidate, native provenance и ограничения |
| `SHA256SUMS` | Контрольные суммы опубликованных файлов |

Сверяйте фактический source commit и результаты с `VERIFICATION.json` выпуска.
Результаты 1.6.0 не являются новой приёмкой 1.6.1. Номер продукта не повышает
версии state/schema/wire автоматически; совместимые legacy readers сохраняются.

Linux LSP требует x86_64/glibc, `/proc` process identity и обычный SIGCHLD без
auto-reaping. macOS/ARM transport не добавлен. Успешные fixture/native/CI checks
не означают проверку любой установленной версии Codex, реального model A/B
или всех пользовательских окружений. Rust выполняет локальные операции;
ускорение inference модели не заявляется.

## Обновление

Сохраните прежний package и private state. Распакуйте новый ZIP в постоянный
каталог, используйте прежние Codex home, skills home и state-dir:

1. `doctor`, затем `install` без `--apply`; просмотрите diff.
2. `install --apply`, затем `verify` с ожидаемым `structural_verified`.
3. Повторный `install --apply` с ожидаемым `unchanged`.
4. Новый процесс Codex, проверка точного handler через `/hooks`, фактического
   SessionStart и применимая `verify --native`.

Команды POSIX/PowerShell, конфликт hooks и rollback — в [SETUP_RU](../SETUP_RU.md).
Публикация релиза сама установленный профиль не обновляет. Hot reload,
фоновый daemon и продолжение после закрытия клиента не добавлены.
