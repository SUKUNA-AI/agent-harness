---
name: agent-harness
description: Условные рабочие процедуры для содержательных задач, делегации, проверок и управления сохранённой работой. Используй для реализации, исследования, ревью, документов и запросов статус/остановка/продолжение длительного запуска; простой вопрос решается напрямую без служебных действий.
---

# Универсальные процедуры — {{VERSION}}

Глобальная политика уже подключена bootstrap; этот skill не условие её действия. Canonical package: `{{PACKAGE_ROOT}}`. Machine pointer: `{{MACHINE_POINTER}}`. Читать только выбранный workflow и его нужные ссылки.

1. При необходимости прочитай `{{PACKAGE_ROOT}}/policies/global.md` и machine overlay из pointer. Не включай приватные runs в контекст других проектов.
2. Выбери workflow в `{{PACKAGE_ROOT}}/workflows/`: direct, research, planning, review, delivery, performance, document, learning, operations, recovery. Смешанная задача может использовать несколько; не создавай тяжёлый процесс для простого вопроса.
3. Проектные правила остаются локальными. Адаптер берётся из применимого контекста; `adapters/domains/README.md` задаёт минимальный предметный контракт. Наличие другого проекта на компьютере не делает его правила применимыми.
4. Для сохраняемой работы основной агент сам создаёт контракт и выполняет `python "{{PACKAGE_ROOT}}/tools/harness.py" init --run-dir <приватный run> --contract <файл>`. Доступны `freeze`, `check`, `evidence`, `gate`, `checkpoint`, `resume`, `status`, `attempt`, `ledger`, `operation`. Перед вызовом неизвестной команды используй её `--help`.
5. На естественные запросы «статус», «останови», «продолжи» прочитай `workflows/recovery.md`, найди runs в machine overlay и используй действующие команды. Пользователь не заполняет JSON/не переносит логи/не обязан открывать терминал.
6. Spawn/follow-up/stop выполняются native инструментами основного чата. Локальный Python не является вторым LLM-движком. Native roles: delivery_owner/helper/reviewer. Если интерфейс не позволяет выбрать custom role, передай её применимые инструкции явно и зафиксируй ограничение discovery.
   В текущем Codex `send_message` лишь доставляет сообщение и не запускает ход завершённого агента. Новую работу/repair для idle или completed ref запускай через `followup_task`; после вызова отличай отправленный запрос от наблюдаемого running/result. Перед сообщением проверь фактический lifecycle.
7. Command checks выполняй реальным runner. Source/content/visual/runtime evidence фиксируй с наблюдаемым proof, точным входом и оценившим агентом; не выдавай их за детерминированные тесты. Финальный gate оркестратор запускает на окончательном candidate без конкурентных writes.
8. Для создания skills используй `harness-skill-creator`, для большой координации — `harness-token-economy`, для подбора tools/коннекторов — `harness-integrations`. Читай их только по задаче. Они не требуют подключать внешние сервисы или запускать агентов для обычного ответа.

Эксплуатация: `{{PACKAGE_ROOT}}/docs/OPERATIONS.md`. Установка/проверка: `python "{{PACKAGE_ROOT}}/tools/harness.py" doctor|install|verify|rollback|uninstall --help`. Версия не распространяется автоматически на уже запущенные сессии или hosted Work.

Для содержательных задач по умолчанию применяй `harness-orchestrate`: сам постановка → краткий план → независимые owners → интеграция → независимая проверка. Python только готовит/проверяет данные; native tools выполняют spawn/follow-up. Последовательный fallback допустим по наблюдаемой причине.
