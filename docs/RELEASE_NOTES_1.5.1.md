# Native Agent Harness 1.5.1

Patch release от 2026-11-02. Он включает исправления независимого аудита A1–A11,
дополнительные устранённые интеграционные гонки и Windows JSON-state recovery.
Тег `v1.5.0` и все его вложения остаются неизменными. Версия 1.5.1 обновлена
согласованно в Python package, installer, Cargo package и Rust protocol;
нативные бинарники должны быть собраны для этой версии, а не переименованы
из предыдущего архива.

## Самое важное

- **Приёмка того же кандидата.** Managed recovery привязывает freeze, checks,
  gate и report к одному staging worktree. Старый PASS с иным root не
  переносится на новый candidate.
- **Правильная остановка процессов.** POSIX owned process group не освобождает
  слот, пока статус живых потомков не разрешён. Неопределённый исход
  фиксируется для восстановления.
- **Учёт токенов.** Только полные валидные счетчики usage подтверждают
  бюджетные ограничения; неизвестный расход не становится нулевым.
- **Recovery без тупика.** Repairable failures и устранённые environment
  blockers проходят разрешённые переходы state machine с сохранением бюджетов.
- **Память без устаревших рекомендаций.** Ledger отбрасывает отозванные
  наблюдения; verified memory требует точного покрытия evidence ссылками.
- **Файловая безопасность.** Ограниченный Python inventory честно сообщает
  неполноту. Linux rename сохраняет поддерживаемые inode flags и проверяет
  inherited ACL до замены; длинные обычные пути не блокируют Ledger как секрет.
- **Windows.** JSON-state writer использует корректную atomic replacement
  семантику для совместимых открытых readers; неизвестные ошибки доступа
  не маскируются успешным состоянием.

Подробные первопричины, ограничения и соответствующие регрессии:
[A1–A11](AUDIT_FIXES_1_5.md).

## Состав и архитектура

Под капотом это автономное дополнение к установленному Codex, а не свой LLM
inference engine: `codex exec` выполняет модельные заходы, Python supervisor
отвечает за процесс, планирование, checkpoint и acceptance, Rust выполняет
интенсивные операции snapshot/RepoMap. Локальное состояние в Strict YAML,
нативный Codex event stream остаётся JSONL. Сервер и база данных не нужны.

Execution Ledger использует идеи Ledger; version-bound memory — MemTrace;
context reducer — AgentDiet; адресное получение деталей — PRO-LONG;
RepoMap queries — Aider и Agent Retrieval Bench; DAG scheduling — AgentSpawn;
independent acceptance и handoff — Anthropic CWC, Superpowers и Pi. Это
самостоятельные реализации выбранных принципов, а не копии исследований или
воспроизведённые заявленные ими проценты ускорения.

[Источники и границы](SOURCES_AND_EVALS.md) ·
[Selected patterns](SELECTED_PATTERNS_RU.md) ·
[Архитектура](ARCHITECTURE_RU.md) ·
[Справочник команд](CLI_REFERENCE_RU.md).

## Что осталось ограниченным

- Ledger govern — advisory для собственных вызовов, не скрытый interception
  всех native tools Codex.
- Token budget проверяется на границах model turns; внутренний inference
  Codex аппаратно не прерывается.
- Пять managed slots — максимум, не обещание ускорения: параллелизм зависит
  от графа задач и доступных доказательств изоляции.
- Memory opt-in, TOON допускается только после положительного измерения;
  проведённый пилот не дал оснований включать его по умолчанию.
- Нативные бинарники поставляются для определённых платформ и содержат
  проверенную цепочку source/hash/license; для остальных требуется сборка.
- Сохранённые user model/provider/reasoning/permissions и compaction
  `500000` не изменяются при обновлении.

## Проверка публикации

Релизный пакет собирается из чистого committed checkout, включает
`release-manifest.json` с хешами всех payload-файлов, отдельные
`SHA256SUMS` и `VERIFICATION.json`. Публикация допустима после CI
Windows/Linux (Python 3.11/3.13 + native), full Python suite и
extracted install/verify/reinstall/rollback. Точное число пройденных тестов,
фактические платформы, SHA источника и хеш ZIP фиксируются в отдельном
verification asset, а не заимствуются из предыдущего релиза.
