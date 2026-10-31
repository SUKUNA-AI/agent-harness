# Локальный MCP для статуса запусков

Это бесплатный локальный read-only connector для одного приватного run store.
LLM API, аккаунт и сеть серверу не нужны. По умолчанию он не установлен и не включён.

В отдельной venv установи пакет с extra: `python -m pip install '.[mcp]'` из корня
checkout. Extra фиксирует официальный `modelcontextprotocol/python-sdk` как
`mcp==2.3.0`; основной harness остаётся без сторонних зависимостей.

Запуск установленного пакета:

```text
python -I -B -m agent_harness.mcp_server --store /path/to/private/run-store
```

Для checkout можно вызвать `python -I -B tools/harness_mcp.py --store <store>`.
`--store` обязателен: это существующий абсолютный путь к одному разрешённому store.
На Windows выбери абсолютный путь своей машины, без junction/symlink в предках.
Для Codex есть [пример конфигурации](../integrations/harness-mcp.example.toml).
Укажи в `command` именно Python из venv, если её executable не находится в PATH.
Сервер не записывает и не исправляет конфигурацию клиента.

Инструменты:

- `list_runs(limit=20, offset=0)` — до 50 кратких записей за страницу.
- `run_status(run_id)` — записанный статус, текущая attempt, checks и owners.

Читаются только `state.json` и `attempts/<attempt>/checks/<check>/result.json`.
Вывод строится по allowlist: ID, enum-статусы, timestamps, digests, checkpoints,
ограниченные exit codes, owner lifecycle flags. Goal, prompts, contracts, cwd,
native refs, команды, текст evidence, stdout/stderr, ошибки, credentials и файлы
артефактов не выдаются. Тексты входов считаются недоверенными данными.

`recorded_status` — сохранённая запись. `evidence_freshness=not_checked` всегда
означает отсутствие сверки actual workspace, хешей файлов, evidence integrity и
native lifecycle. Сервер не вызывает `Run.status()`, resume, gate или runner:
они могли бы читать workspace вне store или менять состояние.
Даже записанные accepted/passed не становятся свежим подтверждением.

Ограничения: JSON до 256 KiB, структура до 8192 узлов/32 уровней, до 2048 entries
при одном directory scan, до 100 owners и checks на run, до 64 KiB JSON-проекции.
SDK может передавать результат одновременно как text и structured content, поэтому
wire envelope больше самой проекции, но также ограничен этими пределами.
Превышение лимита detail-запроса возвращает ошибку; список считает недоступные run
entries как skipped_entries. Это не успешная проверка пропущенных запусков.

Traversal и произвольные пути вместо ID запрещены. Reparse points/symlinks и
hardlinked metadata files отвергаются. Перед чтением проверяется фактический путь
открытого handle: Windows GetFinalPathNameByHandleW, Linux /proc/self/fd,
macOS F_GETPATH. На другой платформе чтение завершается ошибкой. Создание symlink
для тестов зависит от прав ОС. Это защита области чтения, не sandbox против
произвольного кода с теми же правами ОС; сервер должен запускаться доверенным
локальным клиентом, без публикации stdio через сеть.

Проверки без SDK: `python -B -m unittest discover -s tests -p test_mcp_server.py`.
Обязательный runtime smoke с extra: `python -I -B tools/check_mcp.py`.
Он создаёт dummy store, запускает реальный SDK v2 stdio client/server, проверяет
оба инструмента, отсутствие raw данных, traversal rejection и неизменность store.
Без SDK smoke падает; skip не считается успешной проверкой.

API проверен по [официальному SDK v2.3.0](https://github.com/modelcontextprotocol/python-sdk/tree/v2.3.0)
31.10.2026: `MCPServer`, `Client`, `StdioServerParameters`. Smoke не является
подключением к live Codex или проверкой пользовательских private runs.
