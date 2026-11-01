# Эксплуатация

## Обновление на 1.2

Сначала завершите активные команды прежней версии harness: старый процесс не
участвует в новом OS-lock протоколе. Новые `.guard` anchors постоянны; удаление
lease и удаление anchor — разные операции. Не удаляйте anchor у активного процесса.
Git subdirectories одного checkout разделяют workspace lock, реальные worktrees
остаются независимыми. Для non-Git работы разные cooperating callers должны
использовать одинаковый workspace root и общий resource lock namespace.

Rollback/uninstall записывает journal с before/after copies и progress. При
`rollback_failed_compensated` изменения этой попытки компенсированы; при
`rollback_failed_recovery_required` сначала сверяйте перечисленные пути и journal.
Чужие новые bytes не заменяются при компенсации. Per-target guards обнаруживают
изменения между этапами, но не являются filesystem CAS против внешнего редактора.
Symlink/reparse targets установщик отклоняет до замены: используйте обычные файлы
или отдельно выбранный способ установки, сохраняя исходные ссылки.

Обычный `status` сообщает `source_current`, `recorded_status` и
`evidence_current=null`, если artifacts ещё не сверены. Только явный
`status --verify-evidence` проверяет accepted verdict, logs/reports и dependencies:
exit 0 при текущем evidence, exit 1 при stale/непринятом run, exit 2 при ошибке
чтения/параметров. Сохранённый accepted сам по себе не означает свежую приёмку.

Bootstrap подключает короткую политику к новым локальным Codex sessions. Подробный процесс выбирается по задаче; полный doctor перед простым ответом не нужен. Root — основной чат. Native agents управляются его штатными инструментами, Python лишь проверяет/сохраняет инженерное состояние. Внутренний spawn API не имитируется.

## Команды служебного ядра

У каждой команды доступен `--help`. Из любого cwd используйте абсолютный путь `python <package>/tools/harness.py ...`; при установленном Python package — `python -m agent_harness ...`.

| Команда | Назначение |
|---|---|
| `doctor` | Без чтения credentials показывает нужные paths, syntax, executables, file settings и границы неизвестных runtime capabilities |
| `install` | Inspect/dry-run; `--apply` — адресная транзакция с backups/manifest |
| `verify [--native]` | Структура; с native — новый процесс parser и marker в model input, отдельно от поведения UI/агентов |
| `init --run-dir P --contract F` | Новый приватный сохраняемый запуск с зафиксированным контрактом |
| `freeze --run-dir P` | Фиксация candidate inputs перед итоговыми checks |
| `check --run-dir P --check ID [--cancel-file F]` | Реальная команда из контракта, timeout/logs/evidence |
| `evidence --run-dir P --check ID --record F` | Типизированное evidence для source/content/visual/runtime с proof |
| `gate --run-dir P` | Проверка exact contract/attempt/candidate, required checks, scope и integrity evidence |
| `status --run-dir P` | Быстрый source status; сохранённый accepted ещё не подтверждает текущее evidence |
| `status --run-dir P --verify-evidence` | Read-only проверка accepted verdict, артефактов и dependencies; exit 1 если не подтверждены |
| `context pack --root P --include FILE --output F` | Ограниченный private source packet вне source root |
| `context verify --root P --packet F` | Проверка целостности и актуальности включённых файлов; не acceptance gate |
| `checkpoint --run-dir P --note TEXT --next TEXT` | Версионированная точка восстановления |
| `resume --run-dir P` | Сверка сохранённого состояния и входов; не подмена native resume |
| `attempt --run-dir P --contract F` | Новая ревизия контракта/попытка; старое evidence не наследуется как passed |
| `ledger --run-dir P --record F` | Учёт native owners, их references, scopes и общей ёмкости; запись не создаёт агента |
| `operation --run-dir P --id ID --action A --status S --proof TEXT` | Outcome ledger операций: planned/unknown/confirmed/failed |
| `rollback [--apply]` | Отмена последней установочной транзакции с защитой ручных правок |
| `uninstall [--apply]` | Отмена всех сохранённых установок; runs и пользовательские данные сохраняются |

Установочные команды принимают `--package-root`, `--codex-home`, `--skills-home`, `--state-dir`, `--machine-overlay`; verify также `--cwd`. После установки реальный private state находится через `harness-machine.json`/overlay поле `installation_state`, если флаг не передан. Native verify требует доступного Codex executable. Не включайте full access, не меняйте провайдера и не копируйте auth ради прохождения проверки.

`install --max-child-threads N` — отдельная явная авторизация положительного native child cap (например 5). Сначала просмотрите dry-run, затем добавьте `--apply`. Значение сохраняется в machine overlay `preferred_native_child_cap`; обычный reinstall его не сбрасывает к portable default4 и не повышает более строгий пользовательский текущий cap. Четыре logical lanes и cap учитываемых native children — разные параметры. Уже открытая сессия может сохранять прежнюю runtime ёмкость; effective limit подтверждается новым запуском. Никакое изменение локального файла не доказывает backend support пяти детей.

## Длительная работа

Root выбирает private run store из machine overlay, разделённый по проекту/объекту и конфиденциальности. Сохраняются только нужные решения, ограничения, входы/версии, контракты/digests, владельцы/refs, attempts, evidence, patches/commits и следующий шаг. Для короткого вопроса run не нужен. Если пользователь запретил сохранение, не пишите эти данные; ограничение восстановления обозначается явно. Native/app history не контролируется этим кодом.

Общий state пишет только root/одна команда с lock; owners возвращают отдельные результаты. Контракты/gates исполнителю не передаются как свободно изменяемые условия успеха. Локальные locks — координация и обнаружение ошибок, не OS security boundary против процесса с теми же правами.

Status: найти точный run, вызвать `status`, проверить актуальность evidence и показать работающие/ожидающие lanes, blockers и следующий шаг. Несколько совпадений требуют выбора. Stop: согласованно остановить native owners, сохранить их результаты и checkpoint, записать собственные процессы/ресурсы и unknown operations; не удалять файлы. Только подтверждённое закрытие thread освобождает слот по backend semantics; idle и запись cancelled в локальный JSON этого не доказывают.

Resume: открыть новую сессию и попросить продолжение. Сверить `resume`, реальное дерево/объект/версию, native owners и идентичность собственных процессов. Не верить старому PID и не дублировать owner, пока прежний outcome неизвестен. Native follow-up той же сессии — отдельная capability; восстановленный по checkpoint новый агент помечается новой native reference. Автоматический restart после закрытия приложения не обещается.

Перед повтором неизвестного commit/merge/push/deploy/сообщения/remote edit выяснить фактический итог и сохранить proof. В незавершённом check нет полноценного evidence — значит он не passed. Поздний result старой attempt сохраняется историей и не заменяет current_attempt/accepted.

## Изменения и приёмка

Каждому файловому owner назначить точный cwd/scope и отдельный worktree/ветку, если изменения конкурентны. Shared YAML ограничить records, не только путём. Один интегратор пишет integration worktree, последовательно применяет commits/patches и пересобирает generated outputs. Tests/builds/ports/GPU имеют собственные ресурсные группы.

При интеграционном дефекте вернуть тому же owner check ID, candidate revision/snapshot, полный log path и короткую диагностику, ожидаемое поведение и scope. Его checkout безопасно привести к candidate, сохранив прежнюю работу, создать attempt. После исправления повторить затронутые и обязательные интеграционные проверки. Два одинаковых провала без новой гипотезы требуют диагностики, а не бесконечных повторов/нового агента.

На final gate candidate не изменяется конкурентно. HEAD без dirty/untracked snapshot недостаточен. Required check нельзя ослабить в worker result. Source/content/visual/runtime подтверждения не являются command exits и требуют своего proof; непросмотренный артефакт не закрывает visual. Финальное accepted относится к exact candidate/контракту, не переносится на новые входы.

## Диагностика и откат установки

`doctor` не показывает полный environment и не открывает auth. Ошибка TOML оставляет исходный файл целым. Если `install.lock` уже существует, проверь содержимое/живого владельца и итог операции; не удаляй lock автоматически. Если diff включает чужие пути/права или конфликтует с явной политикой, останови затронутую установку.

`verify` может подтвердить structure, parser и model-input marker, но discovery ролей, spawn/follow-up/cwd/result/behavior требуют отдельных native probes. Успех CLI не объявляй проверкой desktop без совпадающего клиентского профиля и наблюдения. `docs/RELEASE_REVIEW.md` хранит фактическую матрицу.

Rollback проверяет backup/installed hashes и новые пользовательские изменения. В инструкции удаляется только неизменённый managed block, сохраняется остальной текст. В TOML восстанавливаются только неизменённые управляемые строки, неизвестные поля сохраняются. Изменённая собственная роль/skill/pointer или правка управляемого блока — конфликт, весь предварительный план не применяется. Разреши конфликт адресным diff; не затирай целый файл старой копией. `rollback` отменяет последний update, `uninstall` всё сохранённое install history. Backups/private runs автоматически не удаляются.

## Перенос и ОС

В переносимый package входят versioned policy/code/tests/schemas/adapters, без private state, абсолютных путей пользователя и credentials. На новом компьютере отдельно выбери CODEX_HOME, skills/state/overlay и проведи install/verify/native сценарии. На Windows не предполагай Bash; на Arch не предполагай apt. Пробелы/Unicode, line endings, executable discovery и cancellation проверяются на каждой реально используемой ОС. Arch остаётся unverified до фактического исполнения.
