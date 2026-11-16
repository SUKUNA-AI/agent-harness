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
| Live OpenCode managed leaf | BLOCKED | `BLOCKED_NOT_QUALIFIED`: эффективные permissions, catalog и подавление subagents не доказаны |
| Live OpenCode exact resume | BLOCKED | Не доказаны session persistence и продолжение после остановки private server |
| Live OpenCode reviewer | BLOCKED | Нет проверенного effective catalog и отрицательной проверки операций реального клиента |
| OpenCode-native multi-agent | NOT_IMPLEMENTED | Нет контроля children, quotas, ownership и recovery |

В среде разработки private server завершился до readiness с
`EPERM: operation not permitted, listen`. Это наблюдалось у `debug config`,
`debug agents` и `session list` в отдельном HOME/XDG без inference. Поэтому
код **не разрешает** реальное выполнение OpenCode на основании mock PASS,
help или self-report клиента. Production `preflight` даёт
`BLOCKED_NOT_QUALIFIED` до создания процесса модели. Нет flag для обхода этой
границы. Fixture verifier существует только в тестовом подклассе.

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
opencode run --standalone --format json --agent harness-leaf PROMPT
opencode run --standalone --format json --session ses_CONCRETE --agent harness-leaf PROMPT
```

Это форма вызова adapter, а не команда обхода `BLOCKED_NOT_QUALIFIED`.
У `--session` в 2.2.5 есть опасная для recovery особенность: отсутствующий ID
может создать новую сессию. До live-квалификации нужны export/lookup,
проверка identity и отказ при отсутствии прежней сессии. `--continue` и
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
dispatch. Это binding созданного fragment, **не доказательство** effective
config, которое могло измениться из других источников.

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
массиве выигрывает. Для managed leaf как минимум нужен безусловный deny
`subagent`; для reviewer — deny `edit`, `shell`, `external_directory`,
`execute` и всех неразрешённых MCP actions. `edit` защищает также `write` и
`patch`. MCP action называется `<normalized_server>_<normalized_tool>`.

Code Mode по умолчанию включён. `codemode:false` у MCP делает tools явными;
deny `execute` убирает Code Mode. Native `policies` могут hard-deny
`tool.use` после permissions и сохранённых approvals; `plugin.use` позволяет
запретить plugins. Все эти ограничения должны подтверждаться на итоговой
конфигурации, tool catalog и попытками запрещённых действий, включая wrapper
tools. Shell с произвольным выполнением программ может расширить authority;
одного deny native `subagent` недостаточно для общей process isolation.

Тестовый dispatcher предоставляет `search_symbol`, `read_file`,
`write_file`, `execute_command`, shell, edit, execute и subagent. Reviewer
видит только первые два; каждый запрещённый вызов отклоняется, файл не
создаётся. Это negative integration test контролируемого fixture,
не live-проверка permissions OpenCode. Реальный reviewer остаётся blocked.

## JSONL, evidence и recovery

Adapter нормализует V2 `step_start`, `text`, `reasoning`, `tool_use`,
`step_finish`, `error` в существующий event vocabulary supervisor.
Каждый event и вложенный part проверяются на один конкретный `sessionID`.
`step_finish` с `tool-calls` закрывает только промежуточный step.
`turn.completed` появляется лишь при чистом EOF после финального `stop`;
runner дополнительно требует exit `0` и подтверждённое завершение дерева.

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

## Официальные источники

- [V2 configuration](https://opencode.ai/docs/config/): schema, источники и precedence.
- [V2 permissions](https://opencode.ai/docs/permissions/): actions, ordered rules, Code Mode.
- [V2 MCP](https://opencode.ai/docs/mcp-servers/): `mcp.servers`, local process, pool, timeout, permissions.
- [V2 policies](https://opencode.ai/docs/policies/): hard deny, plugin/MCP restrictions.
- [CLI и private/shared server](https://opencode.ai/docs/cli/): standalone и session storage.
- [V2 API](https://opencode.ai/docs/api): session lookup/export/interrupt и effective agent/config probes.

Docs обновляются независимо от installed binary. Для следующей версии снова
проверьте help, schema, effective config, отрицательные tool calls и exact
resume; совпадение примера с актуальным сайтом не доказывает поведение клиента.
