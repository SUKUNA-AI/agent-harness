---
name: harness-orchestrate
description: Делегировать независимые части задачи, подготовить briefs и dependencies, вернуть repair прежнему исполнителю в native Codex.
---

# Протокол делегации

Для существенной приёмки используй `{{PACKAGE_ROOT}}/workflows/review.md` и
review packet: независимый reviewer читает критерии, diff и raw evidence без
авторского вердикта. При передаче/compaction — `{{PACKAGE_ROOT}}/workflows/context.md`.
Не копируй всю историю каждому исполнителю.

1. Выведи из запроса goal, разрешения, cwd/scope, inputs и достаточную приёмку. Ограниченную самостоятельную работу веди прямо. При полезном разделении назначай независимых owners автоматически, обычно 2–3; последовательный план может иметь одного. Root готовит служебные данные сам.
2. Используй plan по `schemas/orchestration.schema.json`: общий context по ссылкам/hashes, конкретные read_sections и outputs. Формат и команды — `docs/ORCHESTRATION_RU.md`: `plan validate`, `plan brief`, `plan next`. Не копируй всю историю. При неизменном контракте повторное чтение инструкций не нужно.
3. Перед каждым spawn/follow-up сверяй реальный полный inventory; lifecycle вызовы сериализуй. Учитывай всех открытых потомков, включая idle/done и helpers. Root считается только при `budget.root_counts=true`; у child-only лимита Codex он не занимает дочерний слот. Не подставляй универсальный cap. Переиспользуй подходящий idle ref; done/interrupt не означают close. Helper создаёт root, отдельной квоты у owner нет.
4. Назначай точный cwd/scope, Git writers — отдельный worktree от известной базы. Один writer на checkout; root единственный интегратор. Общие generated outputs сериализуй. Перед downstream/repair обнови изолированную базу безопасно, сохрани dirty work. Текст контекста не обновляет файлы.
5. Возвращай исправление прежнему доступному owner через `plan repair-brief` с actual check/log/current candidate и новой attempt. `send_message` не запускает завершённый ход; используй штатный follow-up. После двух одинаковых провалов без новой гипотезы диагностируй. Replacement/attempt не сбрасывает бюджет.
6. Owner возвращает ready_for_review и evidence. Root проверяет итоговый candidate и затронутые взаимодействия; required unknown/skipped/timeout не passed. Приёмка и достаточное независимое ревью завершают работу без лишних волн проверки. Recovery сверяет files, dependency evidence, процессы, refs и неизвестные операции.

Действуют разрешения текущей задачи и общая политика. Не меняй model/provider/permissions, не запускай публикации/сообщения без основания. Worktree и роль не создают sandbox. Python только проверяет данные; native tools исполняет root. Не обещай фоновую работу или native resume при восстановлении из checkpoint.
