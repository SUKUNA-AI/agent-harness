# Приёмка и виды доказательств

Универсальный harness и первая задача проекта принимаются отдельно. Все verdict привязываются к версии политики/клиента, backend, профилю, ОС, contract digest, exact inputs и evidence. Этот документ задаёт критерии; выполненные результаты находятся в `RELEASE_REVIEW.md` и приватном run store. `not_run`, `unavailable`, `unknown` и `unverified` не превращаются в passed.

## Автоматические fixtures/core

Обязательные проверки установки: dry-run без writes; сохранение неизвестных ключей/комментариев/security/models/чужих ролей; невалидный TOML без порчи; активный override и byte limit; повторный install без дублей; реальные backups/manifest integrity; перенос/Unicode/пробелы; rollback с посторонней ручной правкой и конфликт при правке управляемой области; локальный config не считается Work-настройкой.

Runner/state/gate: реальные argv, timeout/cancellation и process tree; stdout/stderr artifacts, tool versions/fingerprints, resource locks; missing/skipped/failed check; contract digest changed без version bump; stale candidate/attempt/base; tampered/missing logs; dirty/untracked/deleted inputs; scope/protected paths/shared records; single writer; immutable late results/current_attempt; unknown outcome без blind retry; checkpoint и resume с несовпадающим реальным состоянием; один окончательный candidate.

Fixtures доказывают работу этих механизмов на своих входах. Mocks не доказывают native Codex, текст policy не доказывает её загрузку, command exit не доказывает смысловую/визуальную корректность.

## Новый клиентский запуск и наблюдаемое поведение

1. Проверить штатным native parser configuration schema и новым model-input snapshot наличие global bootstrap/version без имени harness в запросе.
2. В новой native сессии простая задача выполняется без детей/run/dependencies.
3. Пустой новый Git project без локального harness, существующий project со своим AGENTS.md и задача вне Git получают общую политику без копирования project adapters.
4. Проверить intent: review/diagnosis/plan не начинают самовольную реализацию; review without findings допустим; запрет сохранения не создаёт managed artifacts; обучение в режиме подсказки не пишет готовую homework.
5. Research содержит проверенные источники/даты и отделённый вывод; документ проверен по содержанию и просмотренному формату; измерения имеют baseline/inputs/repeats и не выдумывают GPU поддержку.
6. Содержательная задача с четырьмя логическими lanes имеет реальных owners/results каждой части в доступной очереди. Зафиксировать requested topology, observed cap/semantics, maximum accounted threads, фактическое число владельцев.
7. Реальный native owner создаётся, работает в заданном cwd, возвращает результат; ошибка возвращается **тому же** owner с актуальной базой, исправляется и повторно проверяется. Discovery custom role и explicit instructions fallback учитываются раздельно.
8. Idle/exhausted slot lifecycle соответствует backend; all helpers/потомки в одном ledger. Локальный status не считается native close.
9. Прерывание/checkpoint и новый контекст сверяют реальные inputs/native state/process identity/unknown operations; результат восстановления не называется native resume без подтверждения той же сессии.
10. Конкурентные writes изолированы; integration-only defect выявляется общей проверкой и возвращается owner. Негативный fixture не портит production/чужую работу.

Перезапуск приложения может потребовать нового сообщения пользователя. Автозагрузка политики и автоматическое продолжение вычислений — разные свойства. Никакой фоновой работы без штатной разрешённой автоматизации.

## Матрица среды

| Среда | Доказательство, необходимое для observed |
|---|---|
| Windows local Codex | Версия клиента/backend, active paths/config, новый prompt input + behavioral session, native agents, полный текущий сценарий |
| Arch Linux local Codex | Реальный запуск тех же tests/install/native probes на Arch с доступными dependencies; до этого unverified |
| ChatGPT Work | Отдельный подтверждённый hosted source постоянных инструкций и runtime acceptance; local TOML не подтверждение |

Финальный отчёт обязан перечислить passed/failed/not_run/unknown отдельно, указать доказательства автоматического подключения, native refs, repair/recovery, install diff/rollback, exact candidate и remaining blockers. Не объявлять установленным созданный файл и не объявлять завершённой приёмку по test plan.
