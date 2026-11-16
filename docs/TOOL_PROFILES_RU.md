# Tool Profiles и MCP

Tool Profile — Strict YAML политика воспроизводимой конфигурации. MCP server
предоставляет инструменты клиенту Codex/OpenCode; `ast-grep`, тесты и компиляторы
остаются обычными CLI. Harness не проксирует модельные MCP вызовы и не создаёт
новую БД, daemon или LLM API. Supervisor сохраняет scopes, slots, review и gate.

## Профили

| ID, version | Назначение | Зависимости |
| --- | --- | --- |
| `baseline`, 1 | Прежний flow | Ни одного нового MCP/CLI |
| `coding`, 1 | Поиск символов | Serena required; ast-grep optional |
| `architecture`, 1 | Архитектурный анализ | Serena и Graphify required; frozen index |
| `reviewer-readonly`, 1 | Независимый reviewer | Только квалифицированный backend и read-only catalog |
| `browser-testing`, 1 | Проверка интерфейса | Playwright required; live browser qualification |
| `documents`, 1 | Конвертация документов | Docling required; local-only qualification |

Наличие шаблона не доказывает установленную зависимость или квалификацию клиента.
Отсутствующие Serena/Graphify/OpenCode не влияют на baseline. Выбор unavailable
профиля остаётся `BLOCKED`; явная `availability: optional` даёт `NOT_AVAILABLE`.
Optional server сохраняет конкретный отказ; отсутствие server не становится PASS.

## Формат

Схема [tool-profile.schema.json](../schemas/tool-profile.schema.json) закрывает
все поля. Неизвестные ключи, повторные IDs, противоречащие allow/deny/required
списки и несовпадение reference version отклоняются.

```yaml
schema_version: 1
id: project-search
version: 1
backends: [codex, opencode]
roles: [worker, reviewer]
availability: required
binding: worktree
cache: private
timeouts:
  startup: 15
  tool: 60
servers:
  - id: symbols
    transport: stdio
    command: [verified-symbol-server, --project, '{worktree}']
    required: true
    allowed_tools: [search_symbol, read_file]
    read_only_tools: [search_symbol, read_file]
    required_tools: [search_symbol]
    forbidden_tools: [write_file, execute_command]
    readiness: live
    binding: worktree
    cache: private
cli: []
```

`verified-symbol-server` в примере — операторский placeholder, не устанавливаемый
пакет. Команда должна соответствовать реально установленному server. Поля
`environment` допускают несекретные значения; секреты задаются только именами
`env_vars`, значения ключей не сохраняются в профиле/checkpoint. Нельзя считать
`readOnlyHint` server авторитетным описанием безопасности: `read_only_tools`
требует отдельного аудита реализации операции.

Из Python: `load_profile('baseline')`, `load_profile(Path('/absolute/profile.yaml'))`
либо reference `{id: project-search, version: 1, path: /absolute/profile.yaml}`.
`profile_digest` вычисляет SHA256 нормализованного полного профиля. Renderer
подставляет только `{worktree}` и `{cache_root}`. Dry-run не создаёт caches,
не меняет auth, модель, provider, sandbox или пользовательские конфиги.

## Живая готовность и effective catalog

`preflight_profile(profile, backend, root, role)` проверяет зависимости до model
slot. Stdio probe создаёт свой contained процесс, выполняет `initialize`,
`tools/list` и post-catalog `ping`, ограничивает bytes/pages/time и останавливает
всё дерево процессов на каждом исходе. Receipt содержит executable/version,
process identity, cwd, live catalog и его digest. Это диагностическое соединение
уже закрыто после probe; оно не доказывает будущее соединение model client.

`client_catalog_verified: false` явно запрещает трактовать диагностический
receipt как effective catalog Codex/OpenCode. Backend должен самостоятельно
квалифицировать ограничения и фактические tools. Reviewer при отсутствии этого
доказательства блокируется. Prompt и read-only hint не заменяют enforcement.
Отрицательные тесты probe проверяют его собственную границу; они не являются
live security qualification внешнего клиента.

Codex renderer использует `mcp_servers`, `enabled_tools`, `disabled_tools`,
`required`, `startup_readiness = "connection"`, cwd и ограниченные timeouts.
`catalog` readiness не подходит mandatory live dependency. [Официальная
справка](https://learn.chatgpt.com/docs/config-file/config-reference).

OpenCode V2 renderer использует `mcp.servers`, `codemode: false`, `pool: false`,
`agents.harness-leaf.permissions` и deny policies для plugins/subagent. Reviewer
получает deny shell/edit/execute/external_directory. Нативные permission
настройки требуют проверки effective precedence и negative tests клиента;
сама генерация JSON не является квалификацией. [Официальные MCP
настройки](https://opencode.ai/docs/mcp-servers/) и
[permissions](https://opencode.ai/docs/permissions/).

## Serena: один worktree — один stdio process

Встроенный рецепт использует `serena start-mcp-server --project {worktree}`.
Перед запуском обязательно проверяется **установленный**
`serena start-mcp-server --help` и наличие `--project`; команда из документации
сама по себе недостаточна. При отсутствии исполняемого файла профиль BLOCKED.
Каждый клиент запускает отдельный stdio process, cwd и project path совпадают
с его harness-owned worktree. `SERENA_HOME` направляется в отдельный private
cache. Один shared HTTP instance для разных активных проектов не допускается.

Serena stateful; [официальная документация](https://oraios.github.io/serena/02-usage/020_running.html)
рекомендует отдельные stdio instances для разных проектов. Fixture integration
проверяет параллельный поиск одинакового имени с разными исходниками и отдельными
PID/cwd/project roots. Это проверка механизма изоляции; live Serena при её
отсутствии остаётся `BLOCKED_NOT_QUALIFIED`.

## Graphify: frozen source и invalidation

`tooling_provenance.source_snapshot(root)` использует существующий harness
snapshot, включая dirty/untracked bytes. `build_graph_index` принимает **проверенный
оператором** argv установленной версии, `{source_root}`, `{index_output}` и точный
version output. Неизвестная команда Graphify не угадывается и не скачивается.

Builder читает отдельную content-addressed копию исходного snapshot, созданную
до worker edits. Проверяются bytes копирования, отсутствие изменений frozen
source builder-ом и актуальность оригинала после build. Индекс публикуется
атомарно по digest вне candidate. Receipt включает repository identity, base
commit, canonical root, snapshot/files digests, tool version/binary/input hashes,
settings digest, index bytes/digest/path и время. Замена исходников, builder или
индекса обнаруживается `verify_graph_index(receipt, current_snapshot)`.

Несовпадение даёт `STALE_GRAPH_INDEX`; `required=False` возвращает только
`HISTORICAL_ONLY` с причинами. Проверка повторно сверяет actual current source.
`preflight_profile(..., graph_index=receipt)` требует полный verified provenance receipt.
Профиль с `index.required: true` без него блокируется до запуска dependency
процессов; при stale binding также BLOCKED. Reviewer получает уже построенный
индекс; build не выполняется внутри reviewer.
Граф не заменяет чтение импортов/вызовов, dependency checks, tests и gate.

## ast-grep

`inspect_cli` проверяет `ast-grep` и `sg`, включая version signature: Linux `sg`
из setgroups не принимается за ast-grep. Никаких npm downloads при preflight.
`ast_grep_migrate` требует явный список файлов, pattern, rewrite, language и
независимые test argv. Сначала проверяет установленный `run --help`, затем:
исходный JSON scan → bounded rewrite → snapshot diff/scope → old/new scans →
целевые tests → повторная scope проверка. Нулевой rewrite exit недостаточен.

Воспроизводимый пример для отдельной fixture с `calc.py`:

```python
from agent_harness.tooling_provenance import ast_grep_migrate
receipt = ast_grep_migrate(
    '/absolute/fixture', ['calc.py'],
    pattern='old_api($A)', rewrite='new_api($A)', language='python',
    checks=[['python', '-c', 'import calc; assert calc.result == 42']],
)
```

`old_api/new_api` должны иметь реальную fixture и соответствующий тест. При
отсутствии CLI результат `AST_GREP_NOT_AVAILABLE`, не успешная миграция.
[Официальный ast-grep quickstart](https://ast-grep.github.io/guide/quick-start.html).

## Playwright и Docling

`browser-testing` описывает isolated context, controlled artifact directory и
отдельное разрешение `allow_test_writes`. Наличие CLI не доказывает browser binary,
context isolation и cleanup. До live qualification профиль возвращает
`BLOCKED_NOT_QUALIFIED`; screenshot/log не заменяет автоматизированный regression.

`documents` имеет явный `local-only|remote` mode, input/output roots, byte/time
bounds. По умолчанию local-only. Наличие Docling CLI и offline env flags не
доказывает запрет сетевых вызовов. До квалифицированного offline conversion runner
local-only возвращает `BLOCKED_NOT_QUALIFIED`; никакого скрытого remote fallback.
[Docling usage](https://docling-project.github.io/docling/usage/).

## Диагностика отказов

| Наблюдение | Receipt |
| --- | --- |
| Нет команды server | `SERVER_MISSING` |
| Команда не запускается | `STARTUP_FAILED` |
| Некорректный initialize | `HANDSHAKE_FAILED` |
| Нет required tool | `MISSING_TOOL` |
| Операция запрещена policy | `TOOL_FORBIDDEN_BY_POLICY` |
| Истёк timeout | `TIMEOUT` |
| Соединение исчезло после catalog | `DISCONNECTED` |
| Только сохранённый catalog | Нет live readiness; dispatch не допускается |
| Неизвестен cleanup процесса | `UNKNOWN_PROCESS_OUTCOME` |

HTTP/OAuth diagnostics в этом probe не реализованы: required HTTP dependency
остаётся `BLOCKED_NOT_QUALIFIED`, optional — явно недоступна без изменения
acceptance. Native client support HTTP не означает qualified harness preflight.

Целевые тесты: `python -m unittest discover -s tests -p test_tool_profiles.py -v`.
Тесты не требуют моделей, токенов, браузера, Serena или Graphify. Fixture tool
versions явно содержат `fixture`; их PASS не квалифицирует реальные клиенты.
