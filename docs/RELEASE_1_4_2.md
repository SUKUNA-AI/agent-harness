# Релиз 1.4.2 — документация, инструкции и полный пакет

[README](../README.md) · [Индекс](INDEX.md) · [Changelog](../CHANGELOG.md)

Версия 1.4.2 включает исправления R01–R14 из 1.4.1 и обновляет документацию
всего комплекта. Публичный CLI и алгоритмы Python/Rust не изменены. Version
metadata согласованы между Python package, installer и native helper;
standalone Harness Assistant plugin имеет отдельную версию **1.3.1**.
Модели, provider, reasoning, permissions и compaction 500000 сохраняются.

## Что обновлено

| Область | Результат |
| --- | --- |
| README | Полный путь от выбора поставки и установки до поиска по коду, долгих задач, проверки и восстановления |
| Навигация | Новый индекс актуальных справочников, процедур агента и исторических отчётов |
| Архитектура | Разделение Codex/Python/Rust/LSP, context routing, выбор backend и восстановление после compaction |
| Установка | POSIX/PowerShell, отдельные пути, повторная установка, update, conflict, rollback, uninstall и recovery |
| Конфигурация | Наблюдаемые источники, правила изменения config, role overrides, child cap и byte/token limits |
| CLI | Полные маршруты, flags/defaults, exit codes и работающий managed-run пример |
| Приёмка | Актуальность receipts, accepted versus verified current, attempt/lock и неизвестный исход операции |
| Инструкции модели | Короткие skills/workflows, рабочие package references и recovery semantics без обязательной бюрократии для простых задач |
| Интеграции | Точные границы optional MCP/plugin/browser/LSP и локального Codex/hosted Work |
| Исторические документы | Пометки о версии и дате с сохранением прежних измерений |

## Диаграммы

Mermaid-исходники находятся рядом с объясняемым механизмом:

- [README](../README.md): выбор прямой работы или делегации и возврат findings.
- [Архитектура](ARCHITECTURE_RU.md): слои исполнения, поиск/контекст, Python/Rust
  routing и восстановление долгой задачи.
- [SETUP](../SETUP_RU.md): транзакция установки и восстановление.
- [AUTOLOAD](AUTOLOAD_RU.md): цепочка инструкций нового сеанса Codex.
- [OPERATIONS](OPERATIONS.md): lifecycle managed run.
- [ACCEPTANCE](ACCEPTANCE.md): проверка и публикация acceptance.
- [LSP](LSP_RU.md): preview, preflight, apply и компенсация rename.

В полном ZIP схемы дополнительно экспортированы в `diagrams/*.svg` и
`diagrams/*.mmd`; `diagrams/index.html` открывается локально без CDN или сервера.
Это документационный export, не новый runtime dependency harness.

## Состав релизного архива

| Путь в ZIP | Назначение |
| --- | --- |
| `agent-harness/` | Полный versioned source package с документацией и templates |
| `agent-harness/bin/linux-x86_64/` | Linux helper 1.4.2, build-info и third-party notices |
| `agent-harness/bin/windows-x86_64/` | Windows helper 1.4.2, build-info и third-party notices |
| `diagrams/` | Автономная галерея и экспорт тех же Mermaid-схем |
| `update-1.4.2.patch` | Source diff от опубликованной базы 1.4.1 к этой версии |
| `APPLY_RU.md` | Порядок применения полной поставки или patch, включая обновление ignored native bundle |
| `VERIFICATION.json` | Commit/tree, версии, фактические проверки, CI и ограничения |
| `SHA256SUMS` | Контрольные суммы остальных файлов архива |

GitHub «Download ZIP» — архив Git-исходников. Он не содержит ignored native
бинарники и внешний release manifest. Для установки без компилятора используйте
полный релизный пакет. Не смешивайте helper 1.4.1 с Python 1.4.2: проверка
version/protocol/binary identity отклоняет такое сочетание.

## Обновиться с 1.4.1

1. Сохраните полный старый пакет и существующий private installation state.
2. Распакуйте новую поставку в постоянный каталог. Сверьте `SHA256SUMS` и
   `python tools/harness.py native status` из корня нового пакета.
3. Выполните `install` с прежними `--codex-home`, `--skills-home`, `--state-dir`
   без `--apply`; проверьте diff. Затем повторите с `--apply`.
4. Выполните `verify`, откройте новую сессию Codex и при доступном клиенте
   проверьте подключение через `verify --native` в отдельном пустом проекте.
5. Сохраняйте backups/manifest для отката. Если возвращён конфликт или recovery
   state, действуйте по [SETUP](../SETUP_RU.md), сохраняя чужие edits.

Patch меняет исходники, а installer обновляет пользовательский профиль. Это
разные действия. После source patch обновите native bundle из ZIP либо явно
соберите его из текущих источников. Не заменяйте только `build-info.json`.

## Как проверяется эта поставка

Для документации проверяются относительные ссылки и anchors, соответствие
команд реальному CLI, установка rendered skill references, воспроизводимый
managed-run пример, Mermaid parsing/render и читаемость схем. В runtime suite
сохраняются обязательные release/unit checks и интеграции из
[CI](../.github/workflows/check.yml): Python на Windows/Ubuntu, optional MCP,
Rust, native scanner/RepoMap, Cargo recovery и реальный Pyright.

Конкретный commit, run URL, исходы и число skips фиксируются в
`VERIFICATION.json` **этого ZIP**. В репозитории нет частных logs или machine paths.
Архив старого аудита не используется как свидетельство запуска новой версии.

Структурная установка не доказывает, что конкретный клиент загрузил инструкции.
PowerShell/native Codex проверки, для которых нет доступной среды, указываются
отдельно от реально выполненных Linux/CI проверок. Набор documentation changes
не подтверждает рост качества модели, новый процент ускорения или экономии
токенов: для этого нужны сопоставимые реальные задачи и измеритель.
