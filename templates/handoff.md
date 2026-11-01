# Handoff текущей задачи

Заполнить кратко в разрешённом private state; это не machine checkpoint.

- Цель и последние уточнения пользователя:
- Разрешения, ограничения и что требует отдельного основания:
- Project / branch / base revision / actual candidate / contract digest / attempt:
- Принятые решения и короткие причины:
- Изменённые файлы и связанные прочитанные разделы:
- Проверки: passed / failed / skipped / not_run / unknown; команда, exit code, candidate и raw evidence path:
- Нерешённые вопросы, воспроизведение и текущая гипотеза:
- Owners / native refs / процессы / внешние операции с неизвестным исходом:
- Незавершённая компенсация: status, recovery receipt/journal, backups и чужие metadata edits:
- Следующий безопасный шаг и критерий завершения:

При восстановлении сверить реальные files/refs/processes/evidence. Старый accepted
не является текущей приёмкой без read-only `status --verify-evidence` в актуальном
scope; summary/handoff без этого флага не проверяют evidence freshness.
