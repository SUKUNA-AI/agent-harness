# Конфигурация Codex и контракт установщика

Справочник релиза **1.6.0**, актуальность **2026-11-09**. Изменения harness
определяет [installation.py](../src/agent_harness/installation.py), инвентаризацию
источников — [diagnostics.py](../src/agent_harness/diagnostics.py).
Синтаксис и runtime-приоритеты клиента сверяются с
[официальным Configuration Reference](https://learn.chatgpt.com/docs/config-file/config-reference)
и [Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents).
Инвентаризация TOML и фактическая конфигурация запущенной сессии различаются.

## Какие файлы за что отвечают

| Источник | Содержимое | Как используется |
|---|---|---|
| `$CODEX_HOME/config.toml` | Пользовательские параметры клиента, agent defaults, MCP | Базовая конфигурация выбранного локального профиля |
| `$CODEX_HOME/hooks.json` | Управляемая SessionStart-группа и сохранённые foreign hooks | Определение hook для штатного trust и запуска новым клиентом |
| `$CODEX_HOME/agent-harness-hook.py` | Owned launcher установленного session adapter | Установщик обновляет свой launcher; файл сам по себе не доказывает invocation |
| Соседний `<name>.config.toml` | Объявленные параметры профиля | `doctor` перечисляет; выбранный `--profile` этой командой не наблюдается |
| Старый `[profiles.<name>]` | Legacy inline profile values | `doctor` показывает как источник, не обещая поддержку формата клиентом |
| `$CODEX_HOME/agents/*.toml` | Custom native-роли | Имя роли определяется полем `name`, а не только filename |
| `[agents.<role>].config_file` | Ссылка на отдельный TOML роли | Относительный путь диагностика разрешает от объявляющего файла |
| Глобальный/project `AGENTS.md` | Правила поведения и проекта | Загружается клиентом по правилам discovery, не является TOML |
| Пользовательский skills home | Повторяемые процедуры и ресурсы | По умолчанию `~/.agents/skills`; подробности по необходимости |
| Machine pointer + overlay | Package/state locations, preferred cap, observed capabilities | Локальные данные harness; не глобальный профиль модели |

CLI/session/project/managed overrides могут влиять на реальный запуск.
Текстовый role contract, наличие TOML или установленного skill сами по себе не
подтверждают выбранную модель, доступный tool или работающую интеграцию.

## Точные изменения `install`

Установщик патчит строки адресно, сохраняет unrelated keys, комментарии, BOM и
поддерживаемые line endings. Он не заменяет `config.toml` примером целиком.
Malformed/duplicate/неподдерживаемая форма управляемого TOML вызывает отказ,
чтобы не переписать пользовательскую структуру приблизительно.

| Поле | При первой установке | При существующем значении |
|---|---|---|
| Корневой `model_auto_compact_token_limit` | Добавить `500000` | Сохранить явное значение |
| `agents.enabled` | Добавить `true` | `true` сохранить; `false` — явный конфликт |
| `agents.max_concurrent_threads_per_session` | Предпочтение по умолчанию `5` | Без explicit flag выбрать более строгий положительный cap |
| Legacy `agents.max_threads` | Не создавать | Нормализовать alias в текущий ключ; сохранить данные для rollback |
| `agents.interrupt_message` | Добавить `true` | Сохранить пользовательский `false`/`true` |
| `agents.default_subagent_model` | Не задавать | Сохранить |
| `agents.default_subagent_reasoning_effort` | Не задавать | Сохранить |
| `model`, `model_provider`, reasoning основного чата | Не задавать | Сохранить |
| `model_context_window`, `tool_output_token_limit`, `service_tier` | Не задавать | Сохранить |
| `approval_policy`, `sandbox_mode`, Windows sandbox | Не задавать | Сохранить |
| MCP, plugins, аккаунты и credentials | Не подключать | Не переносить и не подменять |
| SessionStart в `hooks.json` и owned launcher | Создать managed handler и launcher | Сохранить foreign hooks; duplicate/unmanaged conflict требует явного разбора/adoption |

Если присутствуют оба cap keys, для обычной установки учитываются оба лимита
и предпочтение overlay; затем legacy alias удаляется либо переименовывается.
Неположительные caps и boolean вместо числа считаются ошибкой. `--max-child-threads N`
допустим только для `install`, явно разрешает выбранный положительный cap и
сохраняет предпочтение в machine overlay. Это адресное изменение лимита,
а не изменение model/provider или разрешений.

Новый default **5** применяется при отсутствии сохранённого предпочтения overlay.
Старый overlay с `preferred_native_child_cap=4` либо другой явной настройкой
не мигрирует автоматически на 5. Меньший пользовательский cap (например, 2)
тоже сохраняется. Чтобы осознанно установить 5 в действующем профиле, выполните
`install --max-child-threads 5` с теми же путями: сначала dry-run, затем `--apply`.
Этот флаг обновит canonical key и saved preference; обычная следующая установка
не сбросит их на другой default.

Текущий ключ `max_concurrent_threads_per_session` ограничивает одновременно
открытые spawned-agent threads **без primary/root**. Клиентские возможности,
workspace requirements и runtime availability могут дополнительно ограничивать
spawn. Пять configured child slots не означают обязательные пять агентов
на каждой задаче и не совпадают с числом логических направлений плана.

## Model и effort дочерних ролей

[Шаблоны role TOML](../adapters/codex/agents/) не задают `model`:

| Native-роли | `model_reasoning_effort` | Назначение |
|---|---|---|
| `delivery_owner` | `high` | Ограниченная реализация, проверки и разрешённые исправления |
| `delivery_helper` | `medium` | Узкий вспомогательный результат |
| `delivery_reviewer` | `high` | Независимое ревью текущего candidate и evidence |

Для поддерживаемого клиента сначала разрешаются explicit spawn values, затем
соответствующие `agents.default_subagent_*`, затем параметры parent. После этого
custom role file имеет приоритет для указанных в нём `model` и effort.
Поэтому `medium` в spawn не снижает role, у которой в TOML закреплён `high`.
Пользовательская роль может явно выбрать model/effort; harness не подменяет её
другой моделью ради доступности или скорости.

Есть отдельная тонкость клиента: если explicit spawn/default выбирает модель
без effort, используется default effort выбранной модели; role file только
с `model` сохраняет уже разрешённый effort. Совместимость выбранной модели
и усилия нужно проверить в реальном runtime. `xhigh`/`max` текстом роли не обещаются.

Provider, approvals, sandbox и MCP наследуются от parent; указание этих session
settings в custom role file не создаёт отдельную конфигурацию среды. Service tier
также следует предпочтению root. Ограниченный scope исполнителя и отдельный Git
worktree — правила координации; они не являются OS sandbox.

## Отдельные параметры work supervisor

`work` сохраняет установленный model/provider и reasoning координатора.
Managed nodes задают effort только конкретному `exec/resume`: исполнители
`medium/high`, reviewer `high/ultra`; default зависит от risk. Structured
planner использует `high`, mandatory plan/final reviewers — максимальный
risk frozen plan (`high` → `ultra`). Подробнее — [Dynamic agents](DYNAMIC_AGENTS_RU.md).
При разрешённой edit
phase adapter выбирает per-invocation workspace-write; explicit installed
read-only сохраняется. Managed workers получают локальное отключение native
children, reviewer — read-only. Эти argv overrides не переписывают TOML.

Contract `budgets` задаёт конечные attempts, wall deadline, turn timeout,
managed concurrency (1–12, reviewers≤3 глобально) и optional `max_tokens`. Они не меняют Codex compaction
или native child cap. Reported usage проверяется между turns; unknown usage
при заданном token cap блокирует продолжение. Полные limits —
[CLI](CLI_REFERENCE_RU.md#foreground-work-supervisor).

## Compaction, окно модели и вывод tools

`model_auto_compact_token_limit = 500000` — политика сжатия **активной истории**.
Она не увеличивает контекстное окно модели и не задаёт общий бюджет токенов всех
ходов или дочерних агентов. Явный пользовательский threshold сохраняется даже
при необычном значении; диагностика может предупредить о неположительном пороге
или пороге не ниже объявленного `model_context_window`. Эти предупреждения не
подменяют runtime-семантику конкретного клиента.

`model_context_window` — отдельное поле. Наличие большего числа в TOML не создаёт
недоступную ёмкость модели/provider. Клиент может сжать историю раньше порога,
если его фактические ограничения этого требуют.

При необходимости можно **отдельно и осознанно** ограничить сохраняемый в истории
вывод каждого tool/function, например:

```toml
# Необязательный выбор пользователя; install это поле не добавляет.
tool_output_token_limit = 4096
```

Такое значение ограничивает сохранённый tool output, а не вычисление инструмента,
объём всех файлов evidence или размер финального ответа. Если вывод усечён,
важные логи/receipts сохраняют отдельно и возвращают краткое резюме с путём.
Нельзя считать усечённый результат полным доказательством. Не добавляйте это
поле только ради совпадения с примером и не снижайте compaction threshold ради
неизмеренной «экономии».

[Переносимый TOML-пример](../profiles/runtime-settings.example.toml) содержит
опциональные предпочтения, включая child default effort; это материал для
сравнения. Установщик сам default effort не добавляет. Ни пример, ни роль
не должны переносить чужие credentials и абсолютные пути другой машины.

## Как читать `doctor`

Запустите из package root с теми же каталогами, что при install:

```text
python tools/harness.py doctor --codex-home <codex-home> --skills-home <skills-home> --state-dir <private-state>
```

Разрешённые поля модели: `model`, `model_provider`, `model_reasoning_effort`,
`model_reasoning_summary`, `model_verbosity`, `service_tier`, `profile`; числовые
поля: `model_context_window`, `model_auto_compact_token_limit`, `tool_output_token_limit`.
Отдельно перечисляются child defaults и referenced role config. Неизвестные
provider tables, agent instructions и произвольные секреты в отчёт не копируются;
подозрительные/слишком длинные string values редактируются.

| Поле отчёта | Значение для читателя |
|---|---|
| `source`, `source_kind`, `table`, `declaring_source` | Где объявлено значение |
| `settings`, `agent_defaults` | Разрешённые значения именно этого источника |
| `status=unreadable`, `error_type` | Источник прочитать не удалось; содержимое не раскрывается |
| `invalid_setting_types`, `warnings` | Проблема типа либо ограниченная диагностическая подсказка |
| `declared_name`, `file_stem` | Реальное имя роли и filename; возможное несовпадение |
| `declared_role_name_collisions` | Несколько файлов объявляют одинаковое native-имя |
| `observation=files_only` | Проверены файлы, не текущая сессия |
| `effective_runtime=unknown_until_client_probe` | Active model/profile/overrides не разрешены этой командой |

`verify` дополнительно проверяет installed state и canonical package; `--native`
проверяет startup/model input новым процессом. Фактический spawn с нужной ролью
и выбранные модель/effort требуют наблюдения клиента. Полная последовательность,
пути и recovery — в [SETUP_RU](../SETUP_RU.md).
