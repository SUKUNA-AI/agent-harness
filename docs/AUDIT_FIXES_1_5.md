# Исправления независимого аудита A1–A11

Дата: 2026-11-02. Проверенная исходная база —
[`788fdfd`](https://github.com/SUKUNA-AI/agent-harness/commit/788fdfdc10dbfbe0d2b7f304ccfeec72ac4e26fa).
Это изменение ветки `main` после выпуска 1.5.0, а не переобозначение старого
release ZIP или результатов прошлого аудита.

## Что исправлено

| Пункт | Новый инвариант | Регрессия |
| --- | --- | --- |
| A1 | Core, lock, evidence и report относятся к одному candidate; staging создаётся до recovery core | Early interruption перед staging: original проходит, returned staging падает; результат не принимается |
| A2 | Cleanup owned process group выполняется и после выхода leader; отсутствие leader не освобождает слот | Настоящий POSIX потомок пишет счётчик и игнорирует SIGTERM; после callback failure и возврата supervisor запись прекращается |
| A3 | Полная статистика содержит оба integer counters; неизвестное не равно нулю | Empty/partial/bool/negative usage; zero/zero; legacy accounting и accepted revalidation |
| A4 | Repairable model failure проходит допустимые состояния, history и исходный attempt budget | Failed initial turn → exact resume → accepted; повтор без изменения → NO_PROGRESS |
| A5 | После устранения managed environment blocker выполняется новый preflight без сброса budgets | Отсутствующий suppression proof блокирует spawn; подтверждённый proof позволяет обычное продолжение |
| A6 | Любой cutoff raw inventory делает результат неполным, независимо от расширений файлов | 4097 non-source files перед существующим test file; complete_inventory остаётся false |
| A7 | Inform и govern одинаково исключают withdrawn/superseded/stale evidence | Отзыв command observation запрещает suggest_reuse при прежнем fingerprint |
| A8 | Verified episode имеет уникальное точное покрытие всех evidence IDs | Missing/extra/duplicate refs отклоняются; старые неполные записи не становятся verified при recall |
| A9 | Неподдерживаемый Linux inode flag не теряется молча | Реальный nodump отвергается до staging, bytes/inode/flags оригинала неизменны; обычный extent flag допустим |
| A10 | Inherited metadata обнаруживается до записи bytes в staged inode и до замены originals | Default ACL на backup или replacement даёт безопасный отказ; обе копии каждого файла проверены до первой замены |
| A11 | Подтверждённый local path не превращается целиком в entropy credential; защита секретов сохранена | Default Ledger работает под длинным parent; known/env secrets, opaque components, links и field-name bypass отвергаются |

Во время интеграции также воспроизведена и исправлена гонка создания общего
private parent двумя workers. `FileExistsError` от второго mkdir допустим
только после повторной проверки, что первый создал ordinary directory, а не
файл или ссылку. Read-only candidate validation не создаёт потерянный ownership
store. При настоящем Ledger persistence failure уже остановленный процесс и
наблюдённый usage сохраняются до перехода в неизвестный результат.

## Границы поведения

- Старый acceptance core, связанный с другим cwd, не переназначается молча.
  Старый PASS не считается доказательством для другого staging candidate.
- При недоказанном завершении дерева процессов слот остаётся unresolved.
  Само отсутствие PID лидера не доказывает остановку потомков. POSIX group
  containment не является OS sandbox для процесса, намеренно покинувшего группу.
- Token budget по-прежнему проверяется на границах turns; это не hard cap
  внутреннего inference провайдера. Legacy агрегаты не преобразуются в новые
  достоверные counters: см. [accounting v2](STATE_FORMATS_AND_MIGRATION_RU.md#полнота-usage-после-аудита).
- A6 касается Python fallback без ripgrep/native, A7 — advisory govern API,
  A8 — API и recall памяти. Это не заявление о глобальном перехвате Codex tools.
- A9/A10 покрывают конкретные Linux metadata scenarios. На неподдерживаемых
  атрибутах rename отказывает, а не сбрасывает права или flags пользователя.
- Secret guard остаётся консервативным, а не универсальным распознавателем
  любых секретов. Недоказанные path-like значения не освобождаются от проверки.

## Как повторить проверку

Из корня source checkout с поддерживаемым Python:

```text
python tools/run_tests.py
python tools/release_check.py
```

Адресные regressions находятся в
[`test_audit_execution_regressions.py`](../tests/test_audit_execution_regressions.py),
[`test_audit_context_regressions.py`](../tests/test_audit_context_regressions.py) и
[`test_audit_rename_regressions.py`](../tests/test_audit_rename_regressions.py).
Они используют temporary projects и fake Codex CLI; платные model calls,
публикация fixture repositories и пользовательские credentials не нужны.
Actual process writes и Linux metadata проверяются отдельно от mocked ordering.

Локальный объединённый прогон на Linux/Python 3.12: **531 tests**, итоговый
`OK (skipped=11)`, exit 0; 520 passed, 0 failures/errors, 153.993 s.
Включены 41 новый regression case: 15 execution/recovery, 19 context/evidence
и 7 rename metadata. Skips: 5 Windows-specific, 5 optional Windows runtime
closure и 1 live language server без настроенного executable.
`tools/release_check.py`: passed, 266 files / 8 skills. `git diff --check`: passed.
Это локальный результат, не утверждение о запуске Windows или paid Codex в
этой среде; отдельные native/Windows jobs проверяются в CI по опубликованному SHA.

Независимое ревью исполнения дополнительно проверило старый accepted run,
созданный исходным кодом 1.5.0 с пустым usage. Новый status/resume не подтверждает
его budget: возвращается STALE, исторический PASS не переписывается.
Датированные прежние отчёты и релизные receipts сохранены как история.

Настройки модели/provider/reasoning, permissions, порог compaction 500000,
native cap5, Rust ABI и набор runtime dependencies этим исправлением не меняются.
