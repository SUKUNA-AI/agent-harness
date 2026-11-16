# OpenCode V2: опциональный execution adapter

[Tool Profiles](TOOL_PROFILES_RU.md) · [Приёмка](TOOLING_ACCEPTANCE_RU.md) ·
[Supervisor](EXECUTION_SUPERVISOR_RU.md) · [CLI](CLI_REFERENCE_RU.md)

Codex остаётся backend по умолчанию. `OpenCodeAdapter` использует существующий
contained runner: process identity, private POSIX process group или Windows Job,
таймаут, cancellation, durable JSONL и проверку завершения всего дерева.
Supervisor продолжает владеть DAG, worktrees, scopes, общими квотами 12 managed
sessions и 3 reviewers, freeze, независимыми checks и final gate. Новая БД,
постоянный daemon и дополнительный API модели в harness не появляются.

## Реальная граница поддержки

Интерфейс сверялся с установленным `opencode v2.2.5` и официальными V2 docs.
Другие версии возвращают `unsupported`; V1 не получает конфигурацию V2.
Совпадение версии и flags означает только наблюдение синтаксиса.

| Возможность | Состояние | Основание |
| --- | --- | --- |
| V2 argv и JSONL adapter | SUPPORTED | Автоматизированные integration tests с отдельными CLI-процессами |
| Параллельные leaf workers, isolated cwd, receipts | QUALIFIED для fixtures | Два реальных fake CLI-процесса; это не live-квалификация OpenCode |
| Interrupt и exact session resume | QUALIFIED для fixtures | Новый экземпляр adapter, прежний session ID, прежний cwd и отдельный журнал |
| Native managed leaf qualification | SUPPORTED | Свежие native permission probes и обязательный guard в реальном процессе исполнения |
| Native pre-turn catalog и session restart | QUALIFIED для 2.2.5 | Реальный клиент; qualification намеренно останавливается до provider dispatch |
| Reviewer с read-only tools | SUPPORTED при успешной квалификации | Реальный catalog, отрицательные native probes, guard и проверка runtime receipts |
| OpenCode-native multi-agent | NOT_IMPLEMENTED | Нет контроля children, quotas, ownership и recovery |

Квалификация выполняется core verifier, а не operator flag или входным JSON.
Отсутствие local listen, неподдержанный клиент, drift, чужие plugins или
непроверяемые права дают `BLOCKED_NOT_QUALIFIED`. Ранее ограниченная среда
возвращала `EPERM` на listen; это блокировка окружения, а не успешный smoke.
Fixture verifier существует только в тестовом подклассе. Проверка native client
с локальным scripted provider не подтверждает доступность оплачиваемой модели.

## Квалификация и guard реального исполнения

`with_tool_config(...).qualify(root=..., role=..., evidence_dir=..., timeout=...)`
возвращает новый adapter. До квалификации flags остаются false. Verifier:

1. Закрепляет executable/hash, launcher files, effective fragment, config sources,
   environment digest и физическую identity canonical worktree.
2. Запускает owned `serve --stdio --port 0`, сверяет native `/api/location`,
   выбранный primary agent и запреты native permission API.
3. Загружает единственный core-owned guard plugin. Его JavaScript и manifest
   immutable, находятся вне worktree и связаны с proof через SHA256.
4. В controlled session получает **фактически собранный** pre-turn tool catalog.
   Native `context` hook записывает его и намеренно прерывает запрос до модели.
5. Перезапускает native CLI с тем же `--session`; прежний непредсказуемый marker
   должен остаться в истории. Lookup отсутствующей сессии обязан вернуть 404.

Guard работает также внутри каждого настоящего `run --standalone`. Непосредственно
перед dispatch он проверяет root, agent, роли и точный digest catalog. Native
HTTP hook дополнительно проверяет tools после protocol lowering и body overlays.
Неизвестный transport/WebSocket блокируется. `execute.before` отклоняет tool
calls вне разрешённого набора, даже если их прислал model server вне catalog.
V2 добавляет некоторые native tools, включая `skill` и Git worktree operations,
помимо ordinary permissions. Guard удаляет все незаявленные операции из каждого
model-visible snapshot; управлять harness-owned worktree через native tool нельзя.
Guard не изменяет модель, provider, prompt или ответ.

Runtime JSONL содержит session/model IDs и catalog, без prompts, headers и keys.
Adapter возвращает `catalog_artifacts`; completion закрепляет их SHA256.
`verify_qualification_evidence(..., require_execution=True, session_id=...,
catalog_artifacts=..., execution_log={"path": ..., "sha256": ...})` требует парные
context/before-dispatch receipts именно этой сессии и покрытие каждого native
`step_start` в закреплённом normalized JSONL. При MCP также проверяется наличие
соединений до dispatch и после завершения provider response. Потеря receipt,
изменение schema или отсутствие guard превращает
результат в `unknown_outcome`, а не в SUCCESS. Отдельный diagnostic catalog
никогда не заменяет runtime evidence.

Proof даёт process-local capability на 60 секунд до dispatch. Перед каждым
использованием проверяются inputs и artifact closure. Историческая проверка
успешного execution не зависит от истечения этого admission TTL. При resume
нужна свежая квалификация. Стабильный путь guard зависит от root/role/profile;
attempt nonce и runtime log path передаются отдельно и не меняют profile digest.
`connected_mcp_servers` отражает реальные connected states native client, а не
cached catalog. Необязательный недоступный server может быть исключён перед
квалификацией по fallback policy. Потеря уже подключённого optional server
внутри исполнения пока блокирует результат консервативно.

## Проверенные команды и isolated standalone

Эти справочные команды доступны в 2.2.5 и не запускают model call:

```text
opencode --version
opencode run --help
opencode session --help
opencode session list --help
opencode session export --help
opencode debug config --help
opencode debug agents --help
opencode mcp --help
```

`opencode debug paths` печатает пути без старта сервера. Эта подкоманда не
принимает `--standalone`. Команды ниже используют private server и требуют
разрешённый local listen; диагностируйте их в отдельном HOME/XDG и disposable
проекте, без production credentials:

```text
opencode --standalone debug config
opencode --standalone debug agents
opencode --standalone mcp list
opencode --standalone session list --format json
opencode --standalone session export --sanitize ses_CONCRETE
```

Обычный CLI способен автоматически запустить shared background service.
Adapter всегда выбирает `run --standalone --format json` и не использует
`--server`, `--auto`, `--continue`, `--fork` или собственный `--worktree`.
Worktree уже создан harness. Private server является потомком CLI и подлежит
общему cancellation/cleanup. Сам flag не доказывает точное продолжение:
session storage надо отдельно проверить после перезапуска private server.

Итоговый argv для квалифицированного leaf имеет форму:

```text
opencode run --standalone --format json --agent harness-leaf --title "Harness managed execution" PROMPT
opencode run --standalone --format json --session ses_CONCRETE --agent harness-leaf PROMPT
```

Явный title исключает дополнительный автоматический model call для названия.
Это форма вызова adapter, а не команда обхода `BLOCKED_NOT_QUALIFIED`.
У `--session` в 2.2.5 есть опасная для recovery особенность: отсутствующий ID
может создать новую сессию. Adapter сначала выполняет native lookup/export,
сверяет worktree и отказывает при отсутствии прежней сессии. `--continue` и
пересказ истории не являются exact resume.

## Renderer V2 и конфигурационные приоритеты

Общий Tool Profile переводится в штатную конфигурацию V2. Codex TOML сюда не
копируется. В V2 используются `mcp.servers`, `agents` и массив `permissions`;
имена V1 `permission`, `bash` и `task` не заменяют V2 `permissions`, `shell`
и `subagent`.

Существенная форма isolated MCP:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "source": {
        "type": "local",
        "command": ["installed-mcp-server", "qualified-arguments"],
        "cwd": "/absolute/harness-owned-worktree",
        "codemode": false,
        "pool": false,
        "timeout": {"startup": 30000, "catalog": 30000, "execution": 60000}
      }
    }
  }
}
```

Команда `installed-mcp-server` — placeholder; профиль обязан закрепить
проверенную installed command/version. `pool:false` не разрешает разделять
один изменяемый Serena project между worktrees. У remote MCP иной объект
`type:"remote", url:...`; он не квалифицирован этим local fixture.

`with_tool_config(config, config_path=..., root=...)` делает копию adapter и
передаёт native config через per-process `OPENCODE_CONFIG_CONTENT`. Он
не пишет `~/.config/opencode`, не копирует auth и запрещает profile override
`model`, `providers` и credentials. Конфликт с уже заданным
`OPENCODE_CONFIG_CONTENT` вызывает отказ. При заданном `config_path` adapter
сверяет ordinary JSON artifact, digest и root перед запуском; drift блокирует
dispatch. Квалификация расширяет fragment только проверенным core guard.
Произвольное поле `plugins` через `with_tool_config` по-прежнему запрещено.
Пользовательские/project plugins пока блокируют квалификацию; harness не удаляет
и не отключает их в пользовательской установке.

Субпроцессу передаётся canonical `PWD`, совпадающий с cwd, и удаляется унаследованный
`OPENCODE_SESSION_ID`. В V2 одного `subprocess.cwd` недостаточно: старый `PWD`
может направить native filesystem tools в другой checkout. Native location
проверяется до inference; guard повторяет проверку в реальном процессе.

OpenCode ищет direct project configs до filesystem root, затем `.opencode`
configs. Пользовательский config дополняется project config; системный config
имеет более высокий приоритет. Agents имеют собственные permissions после
global rules. Plugins способны добавлять actions/tools. Поэтому один
сгенерированный fragment не даёт права назвать reviewer изолированным.

OpenCode не имеет model-independent CLI flag reasoning effort. Adapter
отказывает при `reasoning_effort != None`; он не меняет выбранную модель или
variant и не игнорирует требование незаметно. У OpenCode также нет
`--output-schema`: schema final answer проверяет supervisor. Переданный файл
schema валидируется как доступный input; это не обещание provider-level
structured output.

## Permissions и отрицательные проверки

V2 rule имеет поля `action`, `resource`, `effect`. Последнее совпадение в
массиве выигрывает. Все квалифицированные leaves начинают с deny `*`.
Worker получает native `read/glob/grep/edit`, reviewer/planner — `read/glob/grep`.
Shell, `subagent`, `external_directory`, `execute` и неразрешённые MCP actions
запрещены. `edit` защищает также `write` и
`patch`. MCP action называется `<normalized_server>_<normalized_tool>`.

Code Mode по умолчанию включён. `codemode:false` у MCP делает tools явными;
deny `execute` убирает Code Mode. Native `policies` могут hard-deny
`tool.use` после permissions и сохранённых approvals; `plugin.use` позволяет
запретить plugins. `mcp.use` разрешает только servers выбранного профиля.
Все эти ограничения должны подтверждаться на итоговой
конфигурации, tool catalog и попытками запрещённых действий, включая wrapper
tools. Shell с произвольным выполнением программ может расширить authority;
одного deny native `subagent` недостаточно для общей process isolation. Поэтому
managed OpenCode worker не получает произвольный shell: tests/compilers запускает
supervisor через существующие bounded checks. MCP wrappers для произвольного
command/code execution не квалифицируются. Read-only owner должен получить
read-only policy role без изменения своей логической роли или квоты.

Тестовый MCP dispatcher предоставляет `search_symbol`, `read_file`,
`write_file`, `execute_command`, shell, edit, execute и subagent. Reviewer
видит только первые два; каждый запрещённый вызов отклоняется, файл не
создаётся. Live tests запускают настоящий OpenCode с локальным scripted provider,
который присылает запрещённые calls, и проверяют sentinels и отсутствие RPC
записывающим MCP tools. Это проверка настоящего client enforcement с тестовым
endpoint; cloud integration и доступность конкретной модели проверяются отдельно.

Native `skill` может оставаться в catalog даже при deny permission. Guard удаляет
его schema перед model dispatch и запрещает исполнение, чтобы deny был явным
также в model-visible наборе. Глобальные ambient instructions не изменяются.

## JSONL, evidence и recovery

Adapter нормализует V2 `step_start`, `text`, `reasoning`, `tool_use`,
`step_finish`, `error` в существующий event vocabulary supervisor.
Каждый event и вложенный part проверяются на один конкретный `sessionID`.
`step_finish` с `tool-calls` закрывает только промежуточный step.
`turn.completed` появляется лишь при чистом EOF после финального `stop`;
runner дополнительно требует exit `0` и подтверждённое завершение дерева.
Native counters из `step_finish.tokens` суммируются в usage: input включает
uncached input и cache read/write, output — видимый output и reasoning.
Отсутствующие или
неполные counters остаются unavailable/partial, а не превращаются в нули;
существующая token-budget policy требует полного evidence.

Partial JSONL, duplicate keys, повреждённый UTF-8/JSON, конфликтующие session IDs,
неполный step, crash и обнаруженный `subagent`/`execute` не превращаются в
успех: результат `unknown_outcome`, source reconciliation остаётся обязательным.
Failed model event — failure, timeout/cancellation — uncertain outcome.
Человекочитаемый текст и исходный backend event проходят существующий
secret redactor, журналы имеют SHA256. MCP success не заменяет source/test
evidence, scope, review и final gate.

Нет второй системы checkpoints. Resume использует сохранённый supervisor
state, прежний session ID, backend, worktree и tool provenance. Изменение
профиля или worktree — pre-dispatch refusal; неизвестный исход нельзя
исправлять слепым созданием новой сессии.

## Автоматизированная проверка

```text
python -m unittest discover -s tests -p test_opencode_execution.py -v
python -m unittest discover -s tests -p test_opencode_qualification.py -v
python tools/run_tests.py
python tools/release_check.py
```

Adapter tests не требуют API key или оплачиваемой модели. Они запускают
отдельные fake CLI-процессы и проверяют параллельность, явный session resume,
interrupt/restart, повреждения event stream, отсутствие глобальных writes,
profile/worktree drift, negative reviewer dispatcher и fail-closed версии.
Опциональный `HARNESS_LIVE_OPENCODE=1` проверяет установленный help и сохраняет
различие между observed flags и live qualification. Skip не считается
успешной квалификацией.

Для live native enforcement с локальным scripted provider, без cloud tokens:

```sh
HARNESS_LIVE_OPENCODE_QUALIFICATION=1 PYTHONPATH=src:tests python -m unittest test_opencode_qualification.LiveOpenCodeQualificationTests -v
```

Тесты требуют работающий local listen. Они проверяют zero provider requests при
qualification, фактический runtime catalog, запрещённые unsolicited model tool
calls, неизменённые sentinels, отказ MCP write/command и external-directory read.
Controlled skip не является квалификацией. Windows containment сохраняет общий
Job Object path; живой Windows client должен пройти свой qualification отдельно.

## Официальные источники

- [V2 configuration](https://opencode.ai/docs/config/): schema, источники и precedence.
- [V2 permissions](https://opencode.ai/docs/permissions/): actions, ordered rules, Code Mode.
- [V2 MCP](https://opencode.ai/docs/mcp-servers/): `mcp.servers`, local process, pool, timeout, permissions.
- [V2 policies](https://opencode.ai/docs/policies/): hard deny, plugin/MCP restrictions.
- [CLI и private/shared server](https://opencode.ai/docs/cli/): standalone и session storage.
- [Native server hooks](https://opencode.ai/docs/build/plugins/server/): context/HTTP перед dispatch и tool execution.
- [V2 API](https://opencode.ai/docs/api): session lookup/export/interrupt и effective agent/config probes.

Docs обновляются независимо от installed binary. Для следующей версии снова
проверьте help, schema, effective config, отрицательные tool calls и exact
resume; совпадение примера с актуальным сайтом не доказывает поведение клиента.
