# Проверка 1.2.0 — 01.11.2026

База исправлений: `1fa808fd5f5a09178a07b137e734015c16886b54`, tree
`2c74244b745fdf57e228bee0e03a645626c650b8` (1.1.1).
Работа разделена по изолированным worktrees; изменения объединены одним
интегратором. Исходный аудит был read-only; здесь реализованы его F01–F11 и
выбранные файловые процедуры. Native Codex/LLM и реальный browser здесь не запускались.

## Исправления

| Finding | Результат | Основная regression coverage |
|---|---|---|
| F01: два владельца stale lock | Постоянный OS anchor сериализует recovery и всё владение; lease/receipt сохранены | `test_lock_recovery.py`: независимые процессы и pipe schedule; старый код допускает двоих, новый одного |
| F02: skipped required JUnit case принят | Каждый required regex требует successful non-skipped match | `test_runner_audit.py`: negative required skip и positive optional skip |
| F03: duplicate JSON ID скрывает чужую запись | Валидация baseline/candidate shape, ID type и uniqueness до map | Duplicate, missing ID, invalid root, typed IDs, custom validator |
| F04: rollback теряет новую правку | Per-target recheck, привязка планирования к тому же before, guarded active pointer | `test_install_audit.py`: edits после preflight и во время uninstall planning |
| F05: executable hash из неверного cwd | POSIX relative argv/PATH разрешается в effective cwd, probes исполняются там | Относительный ignored executable, PATH, override cwd; Windows native search semantics отдельно |
| F06: пропавшая dependency останавливает независимую ветку | Ошибка локализована зависимыми nodes, независимый node выбирается | `test_orchestration_audit.py`: missing/malformed state и missing contract |
| F07: status обещает целостность evidence без проверки | source_current отделён от evidence_current; явный read-only verification | `test_status_audit.py`: удаление stdout после принятого gate, persistent state неизменён |
| F08: один checkout определяется по-разному | Core/planner используют shared physical checkout identity; Git subdirs разделяют lock | `test_checkout_identity.py`: sibling/nested cwd, real worktrees, symlink alias |
| F09: частичный rollback скрывает повреждённую установку | Journal, guarded compensation, отдельные failure/recovery statuses | Write/unlink после commit и ошибки journal/pointer; чужие edits сохраняются |
| F10: symlink превращается в обычный файл | Symlink/dangling/reparse targets отклоняются до замены | Portable tests, включая имитацию reparse attribute |
| F11: doctor выдаёт filename за имя роли | declared_name отдельно от file_stem, collisions и source inventory | TOML names, child defaults, profiles и внешние config_file refs |

Doctor по-прежнему сообщает `files_only` / `unknown_until_client_probe`:
наличие настроек не доказывает их применение уже работающим клиентом.
Supported builtin JSON IDs: непустые строки и целые числа; bool/null/float/containers
отклоняются. Для другого формата остаётся frozen custom validator.

## Выбранные механизмы

[Полная карта](SELECTED_PATTERNS_RU.md) связывает первоисточники с конкретными
workflows и командами. Реализованы bounded source packet/verify, handoff, файловая
wiki, независимый review packet, браузерная приёмка и offline experiment procedure.
Внешние runtimes и eval frameworks не установлены. Нет БД/embeddings/daemon.
Постоянный bootstrap не расширен; подробности читаются по задаче.

Дополнительное независимое ревью нашло два P2 в новой упаковке контекста:
JSON escaping мог превысить reader limit, а смена parent directory на symlink
позволяла включить файл вне root. Оба сценария теперь отклоняются до публикации;
POSIX reads привязаны к directory descriptors с no-follow на каждом компоненте.
На других платформах используются проверки reparse/path и идентичности файла;
это не OS sandbox. Повторная независимая проверка подтвердила оба исправления.

В installer reviewer дополнительно воспроизвёл окно между вычислением rollback
и повторным чтением initial bytes. Snapshot планирования теперь используется
напрямую; повторный repro сохраняет новую правку и возвращает blocked.

## Проверки

- Полный `python tools/run_tests.py`: **259 tests, OK, 3 platform-specific skips**
  в Linux / Python 3.12.14. Это unit/integration/regression coverage, не LLM eval.
- `python tools/release_check.py`: portable files, metadata, links и 6 skills.
- `git diff --check`: passed.
- Независимые reviews: runner ↔ installer; root integration/status/config;
  новые context workflows/tools и повторная проверка найденных defects.
- GitHub Actions выполняет Ubuntu/Windows × Python 3.11/3.13, включая отдельный
  MCP smoke. Результат конкретного commit проверяется в Actions; этот локальный
  отчёт не подменяет Windows/native runtime qualification.

## Измерение локальной накладной работы

Использован [stdlib profiler](../tools/profile_core.py): 100 файлов по 1100 bytes,
три коротких subprocess checks, freeze и gate, без модели/браузера/сети. Baseline
и candidate чередовались на одной машине: два batches по пять измерений каждого,
перед каждым batch один warmup. Параллельная локальная suite не исполнялась.

| Метрика | 1.1.1 | 1.2.0 |
|---|---:|---:|
| Измерений | 10 | 10 |
| Медиана wall time | 0.580 s | 0.514 s |
| Диапазон wall time | 0.568–0.633 s | 0.495–0.553 s |
| Git queries | 88 | 66 |
| Version probes | 9 | 7 |
| Hash executable | 9 | 7 |
| Полных source snapshots | 11 | 11 |

В этом fixture медиана ниже примерно на **11.5%**, абсолютная разница около 67 ms.
Это не процент ускорения модели или всей пользовательской сессии. Git settings
читаются одним процессом; одинаковые probes и executable hashes разделяют cache
только внутри одной короткой фазы. До/после каждого check хэши и probes свежие.
Regression с самозаменяющимся executable при exit 0 всё равно даёт
`toolchain_changed` и rejected gate. Source freshness checks не удалены.

## Совместимость и оставшиеся границы

Обновление/rollback — в [operations](OPERATIONS.md). Завершите команды старой
версии перед переходом на новый lock protocol. Non-Git callers должны согласовать
общий workspace root/lock namespace; arbitrary overlapping ranges не регулируются
отдельным registry. Guarded rollback не является абсолютным CAS против редактора,
который не участвует в протоколе; unresolved conflicts явно требуют reconciliation.

Planner claims поддерживают exact path и `directory/**`; более широкие core glob
patterns не автоматически становятся допустимым DAG scope. JUnit проверяет
поддержанный testcase-based формат; нестандартный report producer нужно отдельно
квалифицировать либо использовать проверяемый JSON/adapter. Смена parent binding
по-прежнему инвалидирует старую приёмку child; fine-grained reuse не добавлялся.

Порог 500000, пользовательские explicit overrides, model/provider, coordinator
effort, owner/reviewer high, helper medium и security settings сохранены.
Native model end-to-end latency, token/cost savings, реальное compaction/recovery
и сравнительное качество относительно Claude не измерялись. Для таких выводов
нужны реальные сопоставимые задачи по [experiment procedure](../workflows/experiments.md).
