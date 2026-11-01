# Необязательный локальный MCP: статус запусков

Контракт релиза **1.5.0**, сверено **2026-11-01** по
[mcp_server.py](../src/agent_harness/mcp_server.py) и
[SDK smoke](../tools/check_mcp.py). Это read-only stdio server для **одного явно
выбранного private run store**. Основные install, snapshots, RepoMap и code/LSP
команды не требуют этого MCP. Сам server не вызывает модель/LLM API и не требует
аккаунта, сети, БД или постоянно работающего daemon.

## Установка optional extra

Создайте отдельную venv вне репозитория и установите extra из доверенного
package root. Это отдельное намеренное действие: базовый installer его не
выполняет. Pip может обратиться к package index; выполняйте установку, только
когда этот optional connector нужен и сеть разрешена.

POSIX shell, из package root:

```bash
HARNESS_MCP_VENV="$HOME/.venvs/agent-harness-mcp"
python3 -m venv "$HARNESS_MCP_VENV"
"$HARNESS_MCP_VENV/bin/python" -m pip install '.[mcp]'
"$HARNESS_MCP_VENV/bin/python" -I -B tools/check_mcp.py
```

Windows PowerShell, из package root:

```powershell
$HarnessMcpVenv = Join-Path $HOME ".venvs\agent-harness-mcp"
py -3 -m venv "$HarnessMcpVenv"
$HarnessMcpPython = Join-Path $HarnessMcpVenv "Scripts\python.exe"
& $HarnessMcpPython -m pip install ".[mcp]"
& $HarnessMcpPython -I -B tools/check_mcp.py
```

Extra закрепляет официальный `modelcontextprotocol/python-sdk` как **`mcp==2.3.0`**.
Version pin относится к optional MCP; базовый harness остаётся Python stdlib.
`check_mcp.py` запускает SDK v2 client/server на изолированном dummy store;
отсутствие нужного SDK — failed check, а не успешный skip.

## Запуск и подключение клиента

Server принимает только обязательный `--store`: существующий абсолютный путь
к разрешённому локальному run store. На Windows UNC store не поддерживается;
в пути и предках не должно быть junction/symlink. Создание пустого store допустимо,
но оно не создаёт содержательный run и не импортирует историю.

Установленный в venv пакет запускается так:

```text
<venv-python> -I -B -m agent_harness.mcp_server --store <absolute-private-run-store>
```

Из source checkout доступен entrypoint:

```text
<venv-python> -I -B tools/harness_mcp.py --store <absolute-private-run-store>
```

Это stdio process для клиента. Он ждёт протокольный ввод, поэтому простой запуск
в терминале без MCP client не является проверкой tools. Не публикуйте этот stdio
через сеть; server рассчитан на доверенный локальный клиент.

В Codex добавьте **только выбранную таблицу** из
[harness-mcp.example.toml](../integrations/harness-mcp.example.toml). `command`
должен указывать на Python именно этой venv, а `--store` — на один конкретный
store. Для TOML на Windows удобно использовать literal string с обратными
слешами либо нормальный TOML string с корректным escaping. Пример с placeholders:

```toml
[mcp_servers.harness_runs]
command = '<absolute-path-to-venv-python>'
args = ['-I', '-B', '-m', 'agent_harness.mcp_server', '--store', '<absolute-private-run-store>']
enabled_tools = ['list_runs', 'run_status']
```

Placeholders заменяются реальными локальными путями до подключения. Server не
пишет client config, не добавляет другие MCP и не меняет model/provider/security.
После изменения client config откройте новую сессию и проверьте фактический
список tools и первый read-only запрос. Сам TOML даёт configured, а не verified.

## Два доступных инструмента

| Tool | Параметры | Результат |
|---|---|---|
| `list_runs` | `limit=20`, `offset=0`; limit от 1 до 50 | Ограниченная страница записанных run metadata |
| `run_status` | `run_id` | Записанный status, current attempt, checks, owners и checkpoints |

Tools принимают ID, а не произвольные filesystem paths. Читаются только
`state.json` и `attempts/<attempt>/checks/<check>/result.json` внутри выбранного
store. Result строится по allowlist: IDs, enum statuses, timestamps, digests,
ограниченные exit codes, checkpoints и owner lifecycle flags.

Goal/prompts, contracts, cwd, native refs, команды, raw errors, stdout/stderr,
тексты evidence, credentials и файлы deliverables не выдаются. Произвольные
строки входных JSON не становятся доверенными инструкциями. Отчёт про данные
не даёт разрешения выполнять записанные команды.

`recorded_status` — сохранённое состояние. Поле
**`evidence_freshness=not_checked`** означает, что server не сверял actual
workspace, file hashes, integrity evidence и native lifecycle. Даже сохранённый
`accepted`/`passed` не становится свежей приёмкой. MCP не вызывает runner,
`Run.status()`, gate или resume и не выполняет recovery.

Для решения «можно ли продолжать/принимать» используйте разрешённые CLI/gates
и актуальные evidence по [операциям](OPERATIONS.md) и
[приёмке](ACCEPTANCE.md). MCP здесь помогает увидеть запись, не заменяет проверку.

## Bounds и безопасное чтение

| Ограничение | Значение |
|---|---|
| Один metadata JSON | До 256 KiB |
| Структура JSON | До 8192 nodes / 32 levels |
| Directory scan | До 2048 entries |
| Owners и checks одного run | До 100 каждого |
| JSON projection | До 64 KiB |

SDK может передавать projection одновременно как text и structured content;
wire envelope поэтому больше одной JSON projection. Превышение detail bound
возвращает ошибку. `skipped_entries` в списке означает недоступные записи,
а не успешно проверенные runs.

Traversal, произвольные пути вместо ID, reparse/symlinks и hardlinked metadata
files отвергаются. Проверяется фактический путь открытого handle:
`GetFinalPathNameByHandleW` на Windows, `/proc/self/fd` на Linux, `F_GETPATH`
на macOS; на неподдержанной платформе чтение прекращается с ошибкой.
Это собственный reader metadata MCP; его bounds не равны installer limit 16 MiB.
Он ограничивает область чтения, но не создаёт OS sandbox против кода с теми же
правами процесса. Поддержка чтения MCP на macOS не означает поддержку installer.

## Проверка и отключение

Source-only проверки из package root не требуют SDK:

```text
python -B -m unittest discover -s tests -p test_mcp_server.py
```

Runtime smoke требует указанной venv:

```text
<venv-python> -I -B tools/check_mcp.py
```

Smoke проверяет оба tools, valid/negative calls, отсутствие raw/private data и
неизменность dummy store. Он не подключается к live Codex и пользовательским
private runs. Отдельно подтвердите client discovery и фактический scope store.
Для отключения удалите/отключите только добавленную MCP table штатным способом
клиента и завершите соответствующий child process. Run store и install backups
не нужно удалять ради отключения connector.

SDK API исходно проверен по
[официальному v2.3.0](https://github.com/modelcontextprotocol/python-sdk/tree/v2.3.0)
31.10.2026; этот датированный факт не заменяет проверку установленного extra.
[Plugin](PLUGIN_RU.md) и [прочие интеграции](../integrations/README.md)
подключаются независимо.
