---
name: agent-harness
description: Выбрать процедуру проверок, делегации и восстановления Codex для длительной или сохраняемой работы. Ограниченную самостоятельную задачу выполнять напрямую.
---

# Процедуры — {{VERSION}}

Package: `{{PACKAGE_ROOT}}`; pointer: `{{MACHINE_POINTER}}`. Bootstrap уже действует.
Загружай только нужный файл из `{{PACKAGE_ROOT}}/workflows/`:

| Задача | Workflow |
|---|---|
| Ответ, исследование, план, ревью | direct, research, planning, review |
| Код, C++/CUDA/измерения, документы, обучение | delivery, performance, document, learning |
| Внешние действия; статус/остановка/продолжение | operations; recovery |

Прямой задаче достаточно обычной проверки. Для managed run root вызывает
`python "{{PACKAGE_ROOT}}/tools/harness.py" init --run-dir <private-run> --contract <file>`.
Команды и обязательные флаги смотри в `{{PACKAGE_ROOT}}/docs/CLI_REFERENCE_RU.md` или
нужном `--help`; recovery и форматы — `{{PACKAGE_ROOT}}/docs/OPERATIONS.md`.
Не исследуй заново известный интерфейс и не создавай run ради простого ответа.

Для полезной делегации загрузи `harness-orchestrate`; для handoff/длинного контекста —
`harness-token-economy`; для tools — `harness-integrations`; для skills —
`harness-skill-creator`. Политика спорного случая: `{{PACKAGE_ROOT}}/policies/global.md`.

Начни с адресного `rg` и чтения участка. Если нужны связи незнакомого проекта,
добавь `context map --root <project> --query <task>`; map — синтаксическая подсказка,
не полнота или semantic reference. `context read` возвращает bounded текст с hash.
Точные definitions/references и preview rename — через `code` с локальным LSP;
`code doctor` проверяет наличие server. `code apply` проверяет plan, bytes и metadata;
при recovery_required сохрани recovery_receipt и backups. Подробности:
`{{PACKAGE_ROOT}}/docs/LSP_RU.md`. Rust backend виден в `native status`; БД/daemon нет.

Native tools управляют native delivery roles. Отдельный foreground `work`
supervisor запускает штатный Codex CLI; inference выполняет Codex, без
дополнительного LLM API client.
`send_message` передаёт сообщение, новую задачу завершённому агенту запускает
`followup_task`. Если custom role недоступна, передай применимые инструкции и назови
ограничение; обещание в брифе не меняет фиксированный effort role TOML.

В managed run runner выполняет checks, root принимает текущий candidate без
конкурентных writes. Source/content/visual/runtime требуют proof; required
skipped/not_run не passed. `status --verify-evidence` проверяет сохранённую приёмку
read-only, включая ранее принятые optional receipts; `resume` пишет состояние и не
является запросом статуса. Private run_store ищи через pointer/overlay, не смешивай
проекты. Установка/обновление: `{{PACKAGE_ROOT}}/SETUP_RU.md`; конфигурация:
`{{PACKAGE_ROOT}}/docs/CONFIGURATION_RU.md`. Локальная установка не настраивает hosted Work.
