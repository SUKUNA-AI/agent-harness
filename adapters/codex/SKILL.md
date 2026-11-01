---
name: agent-harness
description: Процедуры длительной работы, проверок, делегации и восстановления сохранённого запуска Codex. Ограниченные самостоятельные задачи выполняются без служебного процесса.
---

# Процедуры — {{VERSION}}

Package: `{{PACKAGE_ROOT}}`; machine pointer: `{{MACHINE_POINTER}}`. Bootstrap уже действует. Не перечитывай весь пакет: выбери нужный workflow в `{{PACKAGE_ROOT}}/workflows/`.

| Задача | Workflow |
|---|---|
| Ответ, исследование, план, ревью | direct, research, planning, review |
| Код, C++/CUDA/измерения, документы, обучение | delivery, performance, document, learning |
| Внешние действия; статус/остановка/продолжение | operations; recovery |

Для ограниченной задачи достаточно прямого выполнения и нужной проверки. Для сохраняемой работы root готовит контракт и вызывает `python "{{PACKAGE_ROOT}}/tools/harness.py" init --run-dir <private-run> --contract <file>`. Команды: `freeze`, `check`, `evidence`, `gate`, `checkpoint`, `resume`, `status`, `attempt`, `ledger`, `operation`. Неизвестный интерфейс уточняй через `--help`; известную команду повторно исследовать не нужно. Форматы: `{{PACKAGE_ROOT}}/docs/OPERATIONS.md`.

При полезной независимой делегации загрузи `harness-orchestrate`; при длинном контексте — `harness-token-economy`; для выбора tools — `harness-integrations`; для создания skills — `harness-skill-creator`. Не загружай их все по умолчанию. Подробная политика нужна при спорном случае: `{{PACKAGE_ROOT}}/policies/global.md`.

Для незнакомого большого проекта используй `context map --root <project> --query <task>` после адресного `rg`; карта даёт релевантные symbols/files с hashes и строками. `context read` догружает нужный участок. Точные definitions/references и rename доступны через `code` с локальным language server; `code doctor` показывает наличие. Rename возвращает план, `code apply` сверяет прежние bytes. Rust helper обнаруживается автоматически; `native status` показывает backend. БД и постоянный сервис не запускаются.

Native tools управляют ролями delivery_owner/helper/reviewer; Python не запускает LLM. В текущем Codex `send_message` доставляет сообщение, а новую работу завершённого агента запускает `followup_task`. Если custom role недоступна, передай её применимые инструкции и зафиксируй ограничение. Фиксированный effort role TOML нельзя перебить обещанием в брифе.

В управляемом run командные проверки выполняй runner, финальный gate — на текущем candidate без конкурентных writes. При прямом выполнении используй обычные соразмерные проверки без обязательного run/gate. Source/content/visual/runtime evidence требуют фактического proof. На запросы статуса/остановки/продолжения используй recovery и private store из pointer; пользователь не обязан переносить JSON или логи. Чужие проектные данные не подмешивай. Установка: `doctor|install|verify|rollback|uninstall --help`; локальные файлы не настраивают hosted Work.
