---
name: harness-orchestrate
description: Автоматическая постановка, брифы, зависимости и repair для содержательных задач в native Codex; без второго LLM runtime.
---

# Автоматическая координация

Используй для содержательной задачи с несколькими полезными частями. Короткий ответ или маленькая операция выполняются напрямую. Пользователь задаёт результат; contract/plan/brief и служебные команды готовит root.

1. Определи intent, действующее разрешение, cwd/scope, inputs и достаточную приёмку. План/ревью/диагностика не разрешают product edits. Выбери только нужный workflow и skills из текущего package через machine pointer; не используй чужие проектные данные.
2. По умолчанию назначь двух полезных независимых owners, третьего — при независимой части и ёмкости. Неделимую работу/меньшую ёмкость/native unavailable выполняй последовательно с короткой причиной. Не создавай искусственные задачи ради количества.
3. Создай добавочный plan по `schemas/orchestration.schema.json` с `common_context`, конкретными `read_sections` и `outputs`; local refs проверяются по paths/hashes. Проверь `plan validate`. Используй `plan brief` и `plan next` из package CLI; формат и пределы — `docs/ORCHESTRATION_RU.md`. Бриф печатает `orchestration_binding` для frozen child contract и точный expected child proof. Python выдаёт рекомендации, native tools вызывает root.
4. Наблюдай полный список native threads перед каждой волной и **каждым** spawn/follow-up. Один общий cap включает root/idle/open/потомков. Сериализуй lifecycle вызовы. Не повторяй вызов с неизвестным исходом, не объявляй done/interrupt закрытием. Переиспользуй доступный idle ref с новой последовательной lease. Helper назначает root.
5. Git writers получают отдельные worktrees, один writer на checkout, root единственный интегратор. Сохраняй unrelated changes. Интегрируй dependency перед downstream checkout refresh; контекст не меняет файлы.
6. Верни ошибку тому же доступному owner через `plan repair-brief` с actual check/log/current candidate и новой attempt. При двух одинаковых провалах без новой гипотезы диагностируй. Owner возвращает ready_for_review; independent review и финальный gate идут на текущем итоговом candidate без конкурентной записи.
7. Recovery сверяет checkpoint с actual files/processes/native refs/operations. Unknown/not_run/timeout не passed. Не обещай native resume или работу после закрытия приложения. В финале отдели результат, реальные проверки и ограничения.

Не меняй permissions/security/provider/model, не создавай accounts, внешние публикации/сообщения и telemetry без основания текущей задачи. LangGraph/Langfuse/платные сервисы не обязательны. Доступный connector сначала ищется штатными tools, затем разрешённый fallback; наличие tool не расширяет полномочия.
