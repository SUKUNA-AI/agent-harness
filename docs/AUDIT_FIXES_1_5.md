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
| A11 | Подтверждённый local path не превращается целиком в entropy credential; failure packet остаётся структурированным, защита секретов сохранена | Default Ledger, failed check и exact repair работают под длинным parent, включая настоящий Python-file traceback; known/env secrets, opaque components, links и field-name bypass отвергаются |

Во время интеграции также воспроизведена и исправлена гонка создания общего
private parent двумя workers. `FileExistsError` от второго mkdir допустим
только после повторной проверки, что первый создал ordinary directory, а не
файл или ссылку. Read-only candidate validation не создаёт потерянный ownership
store. При настоящем Ledger persistence failure уже остановленный процесс и
наблюдённый usage сохраняются до перехода в неизвестный результат.

Дополнительный полный прогон под длинным temporary parent выявил A11 также
в error packet: структурированная диагностика превращалась в одну JSON-строку.
Теперь Ledger связывается с отдельным immutable YAML packet по pointer/digest,
а repair prompt сохраняет диагностический sample. Независимый контрпример с
настоящим Python traceback дополнительно проверяет существующие абсолютные
source paths внутри текста: bounded opt-in разбирает только path spans известных
диагностических форм, только внутри candidate/private run. Секреты проверяются
в исходном тексте до такой проверки, каждый компонент пути остаётся проверяемым,
сам packet не переписывается. Ledger-off поведение сохранено.

### Дополнительная проверка Windows state reader

[CI для `0c97f59`](https://github.com/SUKUNA-AI/agent-harness/actions/runs/41401866390)
прошёл в пяти jobs, но Windows/Python 3.13 выявил один отказ
старого cancellation test: атомарная замена `state.json` получила WinError 5
при параллельном опросе состояния. Ошибка возникла при регистрации основного
процесса; более поздняя отмена validator не должна скрывать такой отказ записи.
Лог не устанавливает, какой именно handle блокировал замену.

В старом reader обнаружен соответствующий механизм: обычное открытое чтение
Windows не разрешало delete-sharing. JSON reader теперь открывает файл только
для чтения, но с `FILE_SHARE_DELETE`, чтобы собственный reader не мешал rename
атомарного writer. Это режим совместного доступа, а не изменение ACL или обход
разрешений. См. [контракт CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew).
Владение native handle, CRT descriptor и текстовой обёрткой разделено явно;
дескриптор не наследуется дочерними процессами и закрывается ровно один раз.
UTF-8/BOM и обработка ошибок сохраняются, embedded NUL не допускается.

Детерминированная проверка удерживает reader открытым во время замены:
старое чтение получает прежний целый документ, новое — новый целый документ.
Отказы прав или посторонние несовместимые handles по-прежнему могут остановить
запись. Writer retries, lock, verdict и immutable evidence reader не меняются.

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
Windows JSON reader дополнительно покрыт
[`test_json_state.py`](../tests/test_json_state.py).
Они используют temporary projects и fake Codex CLI; платные model calls,
публикация fixture repositories и пользовательские credentials не нужны.
Actual process writes и Linux metadata проверяются отдельно от mocked ordering.

Итоговый локальный объединённый прогон на Linux/Python 3.12, под длинным
temporary parent: **560 tests**, итоговый `OK (skipped=15)`, exit 0;
545 passed, 0 failures/errors, 200.825 s.
Включены 54 новых regression cases для A1–A11: 15 execution/recovery,
32 context/evidence и 7 rename metadata, а также 16 дополнительных JSON reader
cases. Skips: 9 Windows-specific, 5 optional Windows runtime
closure и 1 live language server без настроенного executable.
`tools/release_check.py`: passed, 267 files / 8 skills. `git diff --check`: passed.
Это локальный результат, не утверждение о запуске Windows или paid Codex в
этой среде; отдельные native/Windows jobs проверяются в CI по опубликованному SHA.

Независимое ревью исполнения дополнительно проверило старый accepted run,
созданный исходным кодом 1.5.0 с пустым usage. Новый status/resume не подтверждает
его budget: возвращается STALE, исторический PASS не переписывается.
Отдельное повторение исходного A11-контрпримера с Python-file traceback получило
ACCEPTED только после второй попытки `exec → resume` и свежей проверки;
девять независимых focused checks подтвердили guard, bounds и ledger-off.
Датированные прежние отчёты и релизные receipts сохранены как история.

Настройки модели/provider/reasoning, permissions, порог compaction 500000,
native cap5, Rust ABI и набор runtime dependencies этим исправлением не меняются.
