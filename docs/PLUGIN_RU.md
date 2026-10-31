# Harness Assistant: plugin из двух skills

Пакет находится в `plugins/harness-assistant/`. Он содержит portable `plugin.json`
и совместимый `.codex-plugin/plugin.json`; все skills лежат внутри пакета.
Согласно [OpenAI Docs](https://developers.openai.com/plugins/build/plugins),
portable identity и `skills/` находятся в корне plugin, а OpenAI overlay — в
`extensions.com.openai`. Здесь hooks явно пустые в обеих формах manifest.
Схема проверялась 31.10.2026; клиентские поверхности могут отличаться по версии.

Plugin не содержит MCP-конфигурацию, hooks, скрипты, модельный routing, permissions
или install actions. Он не заменяет глобальную политику harness и не подключает
MCP автоматически. Локальный metadata MCP включается отдельно по [инструкции](MCP_RU.md).

Установка из локального checkout в поддерживаемом Codex CLI:

```text
codex plugin marketplace add /path/to/agent-harness
codex plugin add harness-assistant@harness-local
codex plugin list --json --marketplace harness-local
```

Marketplace объявлен в `.agents/plugins/marketplace.json`; его source.path
разрешается от корня репозитория. Команды установки меняют выбранный CODEX_HOME:
для изолированной проверки заранее укажи отдельный пустой home штатной переменной
окружения CODEX_HOME. Копирование JSON-файлов не доказывает установку.

После установки открой новую сессию, найди оба skill через список доступных skills
или picker и вызови выбранный skill по имени, которое показывает твой клиент:

- `harness-coordinate` — результат/scope, пропорциональный план, до 2–3 полезных
  потоков в общем native budget, fresh checks и приёмка основным чатом.
- `harness-run-status` — read-only MCP status либо установленный agent-harness
  CLI с явно выбранным разрешённым run. Без этих инструментов сообщает ограничение.

Проверь простой перевод: workflow не должен создавать run или агентов. На задаче
статуса MCP поля recorded_status и not_checked должны сохраняться как ограничения.
Содержательный пример исполнения и native spawning требуют отдельного runtime
наблюдения; structural tests и plugin list их не подтверждают.

Локальные проверки структуры: `python -B -m unittest discover -s tests -p test_plugin.py`.
Они проверяют standalone copy, внутренние ссылки, marketplace root, consistency
manifest и отсутствие hooks/MCP/подстановок machine paths. Native CLI discovery,
установка в изолированный home и реальная активация skills — отдельные статусы;
не называй source tests доказательством этих этапов.
