# Codex: Tool Profiles, MCP и точное продолжение

[Tool Profiles](TOOL_PROFILES_RU.md) · [CLI](CLI_REFERENCE_RU.md) ·
[Приёмка](TOOLING_ACCEPTANCE_RU.md) · [OpenCode](OPENCODE_RU.md)

Codex остаётся основным backend. Контракт без новых execution/tooling полей
сохраняет прежний baseline. Дополнительные MCP подключаются явно выбранным
профилем. Harness использует штатный MCP client Codex и сохраняет существующий
`exec --json`/`exec resume SESSION_ID`; отдельный универсальный MCP proxy не нужен.

## Фактический интерфейс

Интерфейс сверялся с `codex-cli 0.167.0`, help, генерируемой app-server schema
и официальными документами. Проверить установленный клиент:

```text
codex --version
codex exec --help
codex exec resume --help
codex mcp --help
codex app-server --help
```

CLI overrides задаются повторяемым `-c key=value`; значение разбирается как
TOML. В этой версии `-p NAME` добавляет `$CODEX_HOME/NAME.config.toml` поверх
основного user config. Не считайте старые примеры `[profiles.NAME]` описанием
фактического поведения любой установленной версии: сначала проверьте help.
У project config, managed requirements, plugins и CLI overrides собственные
слои precedence. Наличие локального fragment не доказывает итоговый tool set.

Harness не записывает автоматически `~/.codex/config.toml`, не заменяет auth,
model/provider, approvals или пользовательские sandbox settings. Per-run config
и argv фиксируются digest. При конфликте обязательной политики роли с итоговым
клиентом выполнение блокируется; user MCP не должен вернуть запрещённый tool.

Пример native MCP fragment:

```toml
[mcp_servers.source]
command = "installed-mcp-server"
args = ["qualified-argument"]
cwd = "/absolute/harness-owned-worktree"
required = true
startup_readiness = "connection"
startup_timeout_sec = 30
tool_timeout_sec = 60
enabled_tools = ["search_symbol", "read_file"]
disabled_tools = ["write_file", "execute_command"]
```

Executable и аргументы в примере — placeholders. Profile должен содержать
фактические проверенные команды. Сервер Serena выделяется на каждый worker
и привязывается к его canonical worktree; один переключаемый instance для
параллельных проектов не считается изоляцией.

`enabled_tools` задаёт allowlist, `disabled_tools` исключает операции.
`required=true` не допускает тихого fallback обязательного сервера.
`startup_readiness="connection"` требует живого соединения; cached catalog
не заменяет readiness. Server/tool approval modes не заменяют allowlist и
OS sandbox. Не разрешайте write-capable tool reviewer только потому, что
сервер объявил `readOnlyHint`: annotation — недоверенные данные.

## Проверка нативного tool catalog без inference

`codex_tool_catalog.audit_catalog(adapter, config, root, timeout=15)` запускает
краткоживущий `codex app-server --stdio` в существующей process containment.
Он отправляет только initialization, `thread/start` с `ephemeral:true` и
`mcpServerStatus/list`; `turn/start` отсутствует. Native Codex сам выполняет MCP
handshake, фильтрацию и получение каталога.

Probe создаёт disposable CODEX_HOME без копирования пользовательского auth/config.
Server fragment передаётся `-c mcp_servers.NAME=...`, а thread привязан к
указанному worktree. Это отдельная диагностическая среда. Она не подтверждает
каталог будущего обычного `exec`, настройки shell/sandbox или отсутствие
пользовательских plugins в другом процессе.

В installed schema статус запрашивается так:

```json
{
  "id": 3,
  "method": "mcpServerStatus/list",
  "params": {
    "threadId": "CONCRETE_EPHEMERAL_THREAD_ID",
    "detail": "toolsAndAuthOnly",
    "limit": 100
  }
}
```

`threadId` связывает запрос с конкретным runtime MCP connection.
`detail:toolsAndAuthOnly` не запрашивает resources/templates у серверов, где
для этого отсутствует нужная capability. Pagination ограничена, повтор cursor,
duplicate response/server, повреждённый/partial JSONL и server-side request
на approval дают отказ. Диагностика никогда не отвечает одобрением на такие
requests. Timeout и завершение reader/process tree ограничены.

Результат содержит native client identity, binary SHA256, ephemeral thread ID,
canonical root, config digest, catalog digest, имена tools/schema digests,
наблюдаемые `runtimeStatus`, process identity и digests event/stderr streams.
`client_catalog_verified` и `live_connection_verified` оцениваются отдельно.
Для required server отсутствующий tool, ошибка discovery, `runtimeStatus:null`
или состояние кроме `connected` блокируют readiness. Необъявленный сервер и
обнаруженный запрещённый tool также блокируют probe.

`qualified_for_exec:false` и `reviewer_isolation_qualified:false` сохраняются
даже при `status:READY`. Один эпизод успешного app-server audit не связывает
новый `exec` с тем же connection и не доказывает соблюдение всех прав reviewer.
Обязательная readiness должна подтверждаться и на execution boundary.

Без inference не означает отсутствие любой сетевой активности: startup Codex
может предварительно открывать Responses WebSocket, а remote MCP сам требует
сети. Probe не отправляет model turn и не копирует токены. Network failure
remote dependency нельзя объявлять успешной live-квалификацией.

## Проверенная отрицательная проверка

Опциональный live test использует установленный Codex и собственный маленький
stdio MCP с четырьмя tools: `search_symbol`, `read_file`, `write_file`,
`execute_command`. В изолированном app-server фактически доступны первые два.
Прямые запросы `mcpServer/tool/call` для двух запрещённых tools получают
`tool '...' is disabled for MCP server 'fixture'`; sentinel files не создаются.

Проверка выполняется без модели и credentials:

```text
HARNESS_LIVE_CODEX_CATALOG=1 python -m unittest discover -s tests -p test_codex_tool_catalog.py -v
```

В PowerShell установите `$env:HARNESS_LIVE_CODEX_CATALOG='1'` перед той же
Python-командой. Если Codex отсутствует или opt-in не задан, этот live test
skipped с явной причиной. Основной deterministic набор использует отдельные
fake app-server processes и не требует реального клиента:

```text
python -m unittest discover -s tests -p test_codex_tool_catalog.py -v
```

Live отрицательный тест квалифицирует штатную MCP allowlist/dispatcher только
в проверенной app-server среде. Он не квалифицирует shell, arbitrary wrappers,
внешние directories или модельный reviewer в `exec`. Для принятия роли нужны
все её обязательные ограничения, независимые source/test evidence и final gate.

## Managed/native и recovery

Managed workers запускаются в worktrees, которыми владеет harness.
`codex --worktree` не используется: второй nested worktree нарушил бы ownership.
Native Codex mode сохраняется отдельно от managed scheduler. Receipt подавления
native agents не заменяет проверку MCP и наоборот.

Продолжение использует конкретный сохранённый ID:

```text
codex exec --json PROMPT
codex exec resume CONCRETE_SESSION_ID --json PROMPT
```

Это низкоуровневые формы adapter, а для действующей задачи используйте
`work start`/`work resume` с её private run. Harness сверяет executable,
backend, session, profile, mandatory dependencies, worktree и source/index
provenance; проверяет завершение прежнего процесса и сохраняет event history.
`--last`, новая сессия с пересказом и silent profile drift не являются exact resume.

## Диагностика и rollback

Различайте missing executable, startup failure, handshake failure, missing tool,
policy deny, timeout, disconnect и cached catalog без connection.
`codex mcp list` — обзор конфигурации, не подтверждение required live readiness.
Токены и auth не помещаются в YAML/JSONL или native fragments.

Для rollback уберите ссылку на дополнительный profile и вернитесь к baseline
в новом согласованном run. Не меняйте profile внутри exact resume под тем же
digest. Поскольку global config не переписывался, восстановление credentials
не требуется. Сохраняйте старые receipts как историю; drift требует новых checks.

Официальные источники:

- [Codex MCP](https://developers.openai.com/codex/mcp).
- [Codex configuration reference](https://developers.openai.com/codex/config-reference).
- [Codex App Server](https://developers.openai.com/codex/app-server).

Installed help/schema и воспроизведённые native tests имеют отдельное значение:
актуальный текст сайта не заменяет наблюдаемое поведение установленной версии.
