# Статус, остановка и продолжение

Сначала различи форматы run. Для foreground supervisor с `state.yaml`
используй `work status --verify-evidence`, `work report`, `work stop` и
`work resume` с `--run-dir <run>`. Для legacy core с `state.json` применимы
описанные ниже `status`, `handoff`, `checkpoint` и `resume` без `work`.
Эти namespaces не взаимозаменяемы; новый цикл описан в
[Execution Supervisor](../docs/EXECUTION_SUPERVISOR_RU.md).

Обычный `status` проверяет source, но не обещает целостность artifacts:
`evidence_current=null` означает not_checked. Для подтверждения актуальной
сохранённой приёмки вызови `status --run-dir <run> --verify-evidence`; это read-only, без запуска checks
и нового gate. Отсутствие accepted verdict, изменение/исчезновение принятого receipt
(включая optional) или устаревшая dependency не дают evidence_current=true. В сводке различай recorded accepted и verified current.

Компактная передача решений и файловая память описаны в [context](context.md).
Сохраняй handoff при завершении существенного этапа или ожидаемом compaction;
он дополняет machine checkpoint. Новые уточнения пользователя входят в цель.
`handoff --run-dir <run>` выдаёт read-only краткое состояние с note/next последнего
checkpoint; свежесть файлов/evidence по умолчанию неизвестна. Для текущей приёмки
используй `status --run-dir <run> --summary --verify-evidence`. Полный `status`
сохраняется для точечной диагностики; summary не отменяет стоимость snapshot.

Вход — обычный запрос пользователя; ручной skill/JSON/терминал не нужен. Root читает machine pointer → overlay → run_store, ищет run по текущему проекту/объекту и цели. Если подходят несколько, предложи найденные варианты с кратким статусом, не выбирай произвольный последний. Учитывай запрет сохранения и границу конфиденциальности.

`python <package_root>/tools/harness.py status --run-dir <run>` читает состояние без запуска работы. Отчёт: версия/цель, owners/native refs, ожидающие/работающие/принятые lanes, актуальность checks, blockers и следующий шаг.

Остановка: root согласованно останавливает native owners штатными tools, сохраняет lane results и `checkpoint --run-dir <run> --note <решения/ограничения> --next <безопасный шаг>`. Запиши открытые процессы/ресурсы и действия с неизвестным итогом. Собственные процессы останавливаются по подтверждённой идентичности; не удаляй изменения или незнакомые processes/worktrees. Одного локального status недостаточно для освобождения native слота.

Продолжение: `resume --run-dir <run>` сверяет inputs/evidence/принятые dependencies/locks/operations и записывает состояние восстановления; это не read-only команда статуса. Root дополнительно сравнивает реальные native owners и процессы с checkpoint. Git проверяется только если участвовал. Не создавай дубль owner, пока прежний исход неизвестен. Штатный follow-up/resume той же сессии используй при доступности; новый контекст по checkpoint явно назови восстановлением логического owner, а не native resume. При другой базе/контракте создай `attempt --run-dir <run> --contract <file> --reason <новая-гипотеза>`; старые evidence остаются историей, но не засчитываются в новой попытке. Активный workspace/check lease блокирует переход attempt: сначала согласованно заверши/останови текущую работу, не обходи lock правкой state. Обычная новая attempt не расширяет max_attempts и не снимает/отодвигает deadline; общий бюджет сохраняется.

Не повторяй commit/push/deploy/сообщение/удалённую запись с unknown outcome. Сначала выясни состояние и зафиксируй proof через `operation`. Поздний result старой attempt остаётся историей; authoritative current_attempt/accepted меняет root. После аварии незавершённая проверка без полного evidence не passed. Продолжение после перезапуска приложения требует нового сообщения; автоматический background restart не обещается.

При install/rollback или `code apply` с `recovery_required` сохрани точный journal/receipt и backups, сверь bytes, identity и metadata затронутых файлов. Чужая поздняя ACL/xattr не становится owned evidence; неподтверждённую компенсацию не называй восстановлением. Не повторяй apply до сверки фактического результата. Форматы и разные статусы компенсации — [эксплуатация](../docs/OPERATIONS.md), [LSP](../docs/LSP_RU.md) и [CLI](../docs/CLI_REFERENCE_RU.md).
