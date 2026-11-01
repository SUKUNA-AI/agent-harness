---
name: harness-token-economy
description: Организовать работу harness с ограниченным контекстом и бюджетом агентов. Применять при делегации, длинной сессии, handoff/compaction, файловой wiki или запросе экономии ресурсов.
---

# Экономная организация работы

Для handoff, offload outputs, wiki и source packets загрузи
`{{PACKAGE_ROOT}}/workflows/context.md`. Храни проектные знания отдельно от
глобальных skills, только в разрешённой области. Обычные notes могут быть Markdown,
legacy core сохраняет JSON; typed supervisor state и memory используют Strict YAML.
БД, второй runtime и стенограмма не нужны. Sources привязывай к revision/hash.

Начни с адресного `rg` и чтения участка. Если нужны связи незнакомого проекта:
`context map --root <project> --query <task> --max-bytes 8192`.
`--focus-file` ставит файл раньше scan limit, но не гарантирует полноту.
Карта синтаксическая; точные references/rename — через on-demand `code`/LSP.
`context read` даёт bounded текст с hash; смена hash требует нового чтения/навигации.
Rename требует preview и `code apply`; recovery_receipt/backups сохраняй при сбое.
Форматы/лимиты: `{{PACKAGE_ROOT}}/docs/CONTEXT_AND_NATIVE_RU.md`,
`{{PACKAGE_ROOT}}/docs/LSP_RU.md`; флаги — `{{PACKAGE_ROOT}}/docs/CLI_REFERENCE_RU.md`.

Для supervisor run с `state.yaml` используй `work status --verify-evidence`
и `work report --run-dir <run>`; structured handoff находится в
`context/handoff.yaml` этого run. Legacy команды ниже относятся к `state.json`.
Форматы и routing: `{{PACKAGE_ROOT}}/docs/EXECUTION_SUPERVISOR_RU.md`.

Для существующего legacy run начинай с `handoff`; текущую приёмку проверяй через
`status --verify-evidence`. `status --summary` сокращает output, но всё равно
сверяет source; это не дешёвый polling. Полные логи/evidence держи отдельно,
в контекст возвращай status, exit code, нужные строки и path. Пропуски/excerpt
не доказывают успех. Большой PDF сначала исследуй по структуре/поиску.

Сохраняй model/provider, усилие координатора и defaults/overrides ролей.
Механика — medium, реализация/ревью — high; фиксированный effort role TOML
сильнее spawn effort. Не имитируй неподдержанное переключение.
Простую задачу выполни сам. Для полезной делегации обычно достаточно 2–3 потоков
в общем project/runtime cap, включая открытых/idle потомков и helpers.
Передавай scope, нужные read_sections, inputs/checks и ссылки на общий private
контекст; не всю историю. Git writers — отдельные worktrees; интегратор один.
Финал owner — до 20–25 строк, подробности — в evidence.

Объединяй независимые чтения/проверки; считай/валидируй scripts. Не перечитывай
неизменные инструкции. Долгую команду запускай один раз, жди штатное завершение;
проверь exit code и outputs. Повторяй проверки после релевантных изменений/сбоев.
Планируй ближайший этап; остановись после приёмки. Checkpoint/handoff сохраняй
на границе этапа или перед compaction с версиями и следующим шагом. Новый контекст
не называй native resume. Порог автосжатия 500000 и явные overrides сохраняй.
Сокращение текста измеряй в bytes; ускорение/экономию tokens — только в реальных
сопоставимых запусках.
