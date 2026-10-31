# Agent Harness 1.1.0

Переносимые рабочие правила и детерминированные служебные команды для **локального Codex**. Оркестратор и исполнители — native агенты клиента; Python не вызывает LLM, не создаёт сервер и не заменяет Codex. Установленная глобальная политика выбирает глубину процесса под обычный запрос; простой вопрос не требует агентов или каталога запуска.

Canonical policy: `policies/global.md`; workflows — условные процедуры; domains — отдельные адаптеры; проектные адаптеры подключаются отдельно; пути компьютера — machine overlay; приватные runs и backups находятся вне portable package. Версия политики и установщика — 1.1.0. Подтверждённый охват и фактические проверки описывает `docs/RELEASE_REVIEW.md`; этот README не является доказательством установки или поддержки ОС.

В 1.1.0 усилие координатора сохраняется по явному выбору пользователя; native роли содержательной реализации и ревью используют `high`, узкий helper — `medium`. Для механической работы используйте helper либо встроенного агента с `medium`. Длительные команды ожидаются без частого опроса, после завершения проверяются журнал, код выхода и выходные файлы. Установщик по-прежнему не меняет общий model/reasoning: машинные значения по умолчанию настраиваются отдельно по запросу пользователя.

## Установка

Нужен Python 3.11+ (stdlib). Для native verify — установленный Codex. Git/Node/Go/браузеры нужны только соответствующим задачам. Распакуйте пакет в выбранный каталог; не переносите auth, credentials и чужую историю. В примерах `<package>`, `<codex-home>`, `<skills-home>` и `<private-state>` заменяет агент по обследованию, пользователь не обязан вести JSON вручную.

```text
python <package>/tools/harness.py doctor --codex-home <codex-home> --state-dir <private-state>
python <package>/tools/harness.py install --codex-home <codex-home> --skills-home <skills-home> --state-dir <private-state>
python <package>/tools/harness.py install --codex-home <codex-home> --skills-home <skills-home> --state-dir <private-state> --apply
python <package>/tools/harness.py verify --codex-home <codex-home> --skills-home <skills-home> --state-dir <private-state> --native --cwd <empty-project>
```

Первый `install` — dry-run с точными paths, hashes и адресным diff; второй применяет уже проверенный план с backups/manifest. Только изменяемые файлы резервируются. Установщик сохраняет неизвестные ключи/комментарии TOML, старые инструкции и пользовательские роли. Меняются только `agents.enabled`, cap `agents.max_concurrent_threads_per_session` и отсутствующий `agents.interrupt_message`. Portable default — 4 дочерних thread; более строгий существующий cap и пользовательское `interrupt_message=false` сохраняются. `enabled=false`, неподдерживаемый inline/quoted agents table, невалидный TOML и совпадение имени чужой роли — blocker, без перезаписи. Sandbox/approval/model/provider/network/credentials не меняются.

Для явно разрешённого пользователем машинного лимита, например **пять дочерних потоков**, используйте `install --max-child-threads 5` сначала как dry-run, затем с `--apply`. Флаг принимает положительное целое и явно разрешает повышение cap. Желаемое значение сохраняется в machine overlay `preferred_native_child_cap`; plain reinstall не возвращает его к 4. Без флага последующий более строгий пользовательский cap не повышается. Лимит native children отличается от четырёх **логических направлений** и фактической ёмкости уже запущенного runtime; горячее изменение старых сессий не обещается. Несогласованный legacy `agents.max_threads` требует отдельного адресного разрешения alias, а не скрытой записи четвёртого config key.

Непустой `AGENTS.override.md` имеет приоритет; иначе управляемый блок добавляется в `AGENTS.md`. Устанавливаются short bootstrap, machine pointer, три собственных standalone native role TOML и четыре skills: `agent-harness`, `harness-skill-creator`, `harness-token-economy`, `harness-integrations`. Полные логи, workflows, private project content не вкладываются в каждый prompt. По умолчанию CODEX_HOME берётся из environment или `~/.codex`; персональные skills — `~/.agents/skills`. Machine overlay можно задать через `--machine-overlay`; существующая schema 1 сохраняется. Настоящий private state необходимо выбрать явно на подходящем диске.

`verify` без `--native` доказывает только структуру. Native verify запускает новый процесс штатного parser и `debug prompt-input`, подтверждает маркер политики в model input и сохраняет hashes вывода. Он не доказывает поведение desktop, discovery custom role, реальный spawn и успешную задачу — их проверяют отдельно. Новая политика не гарантирует hot reload старого чата. ChatGPT Work не читает эту локальную установку; граница описана в `adapters/chatgpt-work/README.md`. После установки doctor/verify/rollback/uninstall без `--state-dir` находят реальный каталог из installed pointer/overlay `installation_state`; расположение private state не предполагается соседним с package.

## Повседневная работа

Откройте новую сессию локального Codex и сформулируйте цель без стартового промпта:

- «Исправь эту ошибку и проверь результат».
- «Проверь этот PR, ничего не меняй».
- «Сравни варианты по актуальным источникам и обоснуй выбор».
- «Переведи приложенный раздел полностью, сохранив термины и формат».
- «Помоги разобраться с заданием, пока без готового решения».

Для продолжительной работы агент создаёт контракт/состояние и сообщает run reference. «Покажи статус этого run» читает состояние; «Останови этот run в безопасной точке» сохраняет checkpoint и согласованно останавливает свои процессы; «Продолжи выбранную задачу по сохранённому состоянию» сверяет реальные inputs, owners и неизвестные операции. Если подходящих runs несколько, агент предлагает выбор. Команды описаны в `docs/OPERATIONS.md` и `workflows/recovery.md`. Для пользователя терминал и ручные JSON не обязательны.

## Обновление, перенос и удаление

Перед обновлением измените версию политики/установщика согласованным diff, запустите tests и dry-run. Reinstall без изменения версии/файлов не создаёт дубликатов. Изменение управляемого участка пользователем вызывает конфликт; его правка не стирается. Backups и manifests остаются в private state, не коммитятся в переносимый пакет.

```text
python <package>/tools/harness.py rollback --state-dir <private-state>
python <package>/tools/harness.py rollback --state-dir <private-state> --apply
python <package>/tools/harness.py uninstall --state-dir <private-state>
python <package>/tools/harness.py uninstall --state-dir <private-state> --apply
```

`rollback` отменяет последнюю установочную транзакцию, `uninstall` последовательно все сохранённые версии, сохраняя посторонние правки. Предварительно проверяется вся затронутая область и целостность backups; конфликт прекращает запись. Оба действия без `--apply` — dry-run. Не удаляются broad directories, репозитории, runs, неизвестные worktrees или процессы. Значимые пользовательские артефакты требуют отдельной политики очистки.

Перенос пакета: скопируйте только tracked portable package, запустите tests в новом расположении, укажите новый private state и выполните новую установку/verify. Исходники core не меняются. Абсолютные пути обновляются через overlay/installer. На Arch используйте доступный `python`/`python3` и штатные средства системы; не предполагайте apt, Bash на Windows или автоматический WSL. Выполнение тестов на Windows не подтверждает Arch — до реального прогона она unverified.

## Проверки

```text
python -m unittest discover -s tests -v
```

При запуске из source checkout установите `PYTHONPATH=<package>/src` либо используйте изолированную editable install согласно `pyproject.toml`. Для fixtures используйте TEMP/TMP/TMPDIR на разрешённом диске. Tests проверяют runner/state/gate/install и отрицательные случаи; они не заменяют native runtime/behavioral probes. Матрица приёмки — `docs/ACCEPTANCE.md`.


## Состав переносимой поставки 1.1.0

- `adapters/codex/bootstrap.md` и `policies/global.md` — общий промпт и политика.
- `workflows/`, `roles/`, `schemas/`, `src/`, `tools/`, `tests/` — процедуры и рабочее ядро.
- `skills/` — создание skills, экономия ресурсов и выбор интеграций.
- `integrations/` — каталог возможностей и шаблон наблюдений без credentials.
- `profiles/` — безопасные примеры личных правил и настроек, применяются отдельно.
- `provenance/source-snapshot.json` — хэши исходной 1.0.1 до экспортных правок.
- `docs/RELEASE_REVIEW.md` — объём ревью и реально выполненные проверки.

Это экспорт текущей 1.0.1 с незакоммиченными исправлениями, оформленный как 1.1.0:
добавлены переносимые skills и их транзакционная установка. Убраны проектные
адаптеры и локальная конфигурация окружения. Текущая установленная настройка
исходного компьютера этим экспортом не изменялась.

`harness-skill-creator` самодостаточен и может использовать штатный `$skill-creator`,
если тот доступен. Системные skills OpenAI и сторонние плагины не копируются;
skills для документов, браузера и т. п. подключаются по реальной задаче.

Из source checkout проверки можно запускать без установки Python package:

```text
python tools/run_tests.py
python tools/release_check.py
```

Рекомендуемый способ использования — clone/source checkout, затем `tools/harness.py`.
Wheel не является полной переносимой поставкой: policy/workflows остаются рядом
с source. Архив или clone содержит весь комплект. Установка — сначала dry-run,
затем `--apply` с явным private state вне checkout. Не копируйте пример TOML поверх
своего config. Существующие одноимённые чужие skills вызывают конфликт; installer
не принимает их во владение и не перезаписывает. Native verify и поведение агентов
в новом профиле проверяются отдельно; автоматическая готовность всех интеграций
или поддержка другой ОС этим выпуском не заявляется.

Если в новой версии удалён/переименован ранее установленный skill или его ресурс,
reinstall и verify останавливаются: старый skill не должен оставаться активным
незаметно. Проверьте dry-run uninstall старой установки и сохраните нужные ручные
правки, затем выполните разрешённую миграцию. Пакет не удаляет такие файлы сам.
