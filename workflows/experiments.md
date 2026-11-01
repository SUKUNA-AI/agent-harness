# Ограниченные эксперименты с prompts и skills

Применяй при разрешённой оптимизации поведения. Это offline работа, не обязательный
цикл перед обычной задачей. Native Codex остаётся исполнителем. Self-modifying
runtime, БД и внешний eval framework не нужны.

1. Зафиксируй baseline revision, failure mode, гипотезу и одну изменяемую часть.
   Сохрани смысл задачи, полномочия и критерии. Используй
   [карточку эксперимента](../templates/experiment.json).
2. До изменения раздели development cases и holdout. Для каждого сохрани задачу,
   fixture, проверяемый результат и фиксированный evaluator с hash. Включи целевой,
   близкий нецелевой, missing-tool и recovery сценарии. Holdout не подмешивай в prompt.
3. Задай max candidates, max trials, deadline и доступный бюджет native запусков.
   Замена агента/сессии не обнуляет бюджет. Превышение завершает experiment с
   сохранением результата, не запускает бесконечное улучшение.
4. Сравни baseline/candidate на одинаковых cases, model/provider/effort, tools и
   лимитах. JSONL/JSON receipts: case/repeat/split/revisions, outcome, wall time,
   checks и outputs. Tokens/cost — unknown, если runtime их не сообщил.
5. Принимай candidate по заранее выбранному критерию качества без нарушения
   обязательных ограничений, затем один раз проверь holdout. Сохрани keep/discard
   с причиной. При проигрыше верни только собственный diff, без reset/stash чужой
   работы. Изменение verifier означает новый эксперимент.
6. Структурная/лексическая проверка не доказывает поведение. Несколько задач —
   диагностический pilot, не доказательство общего превосходства модели. После
   достаточного результата заверши цикл; сохрани технический вывод в wiki:
   условия, наблюдение, применимость и оставшиеся неизвестные.

Используются механизм bounded experiment/keep-or-discard из autoresearch и
feedback + holdout из GEPA/gskill; полные stacks не устанавливаются. Не добавляй
Promptfoo, NVIDIA SkillEvaluator или Harbor в dependencies/процесс.
