---
name: harness-orchestrate
description: Делегировать независимые части задачи, подготовить briefs и dependencies, вернуть repair прежнему исполнителю в native Codex.
---

# Протокол делегации

Ограниченную самостоятельную задачу выполняй прямо. При полезном разделении
назначай owners автоматически, обычно 2–3; неделимую работу веди последовательно.
Root сам готовит plan по `{{PACKAGE_ROOT}}/schemas/orchestration.schema.json`:
цель/разрешения, cwd/scope, inputs, criteria, read_sections, outputs и общий context
по ссылкам/hashes. Не копируй всю историю и не перечитывай неизменные инструкции.
Helpers `plan validate`, `plan brief`, `plan next`, `plan repair-brief` описаны в
`{{PACKAGE_ROOT}}/docs/ORCHESTRATION_RU.md`; точные флаги —
`{{PACKAGE_ROOT}}/docs/CLI_REFERENCE_RU.md`.

1. До spawn/follow-up сверь полный native inventory; lifecycle вызовы сериализуй.
   Учитывай открытых/idle/done потомков и helpers; done/interrupt не означает close.
   Root считается при `budget.root_counts=true`; child-only лимит не включает его.
   Не выдумывай cap. Переиспользуй подходящий idle ref; helper создаёт root,
   отдельной квоты у owner нет.
2. Git writer получает отдельный worktree от известной базы и точный scope.
   Один writer на checkout, один root-интегратор; generated outputs сериализуй.
   Перед downstream/repair безопасно обнови базу, сохрани dirty work: сообщение
   с новым контекстом не обновляет файлы.
3. Repair верни прежнему доступному owner через `plan repair-brief`: actual
   check/log/candidate и новая attempt с причиной. `attempt --reason` не обходит
   active check lease, max_attempts или deadline. После двух одинаковых провалов
   без новой гипотезы диагностируй; замена агента бюджет не сбрасывает.
   `send_message` передаёт сообщение, новый ход запускает штатный follow-up.
4. Owner возвращает ready_for_review и raw evidence. Root принимает итоговый
   candidate и взаимодействия; required unknown/skipped/not_run/timeout и
   невыполненный testcase не passed. Проверяй сохранённую приёмку через
   `status --verify-evidence`: все принятые receipts, включая optional, и dependencies
   должны оставаться актуальными. Неизменный контракт не заменяет утраченную evidence.

Для существенной независимой проверки загрузи `{{PACKAGE_ROOT}}/workflows/review.md`:
reviewer получает критерии/diff/raw evidence без авторского вердикта. Для handoff —
`{{PACKAGE_ROOT}}/workflows/context.md`; для refs/processes/unknown operations —
`{{PACKAGE_ROOT}}/workflows/recovery.md`. После достаточной приёмки заверши работу.
Сохраняй model/provider/permissions и полномочия внешних действий. Роль/worktree
не создают sandbox; Python проверяет данные, native tools вызывает root.
Не обещай фоновую работу или native resume по checkpoint.
