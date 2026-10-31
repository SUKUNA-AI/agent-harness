# Готовые решения и выбор для общего harness

Проверено 31.10.2026: **37 canonical GitHub repositories в 7 группах**. Для всех
зафиксированы metadata, SHA и README; для 34 найдены root license snapshots.
Четыре harness и четыре смежных инструмента разобраны по профильным исходникам,
остальные прошли screening. Это ограниченный обзор, не полный каталог GitHub и
не результат запуска каждого продукта. Точные pins и источники —
[upstreams.json](../provenance/upstreams.json). Stars показывают популярность.

## Практический выбор

| Кандидат | Что применимо | Решение для этой поставки |
|---|---|---|
| [Superpowers](https://github.com/obra/superpowers) | Проверка перед завершением, debugging, review, создание skills | Используем принципы свежих evidence и независимого review; целый bootstrap и model overrides не импортируем |
| [ECC](https://github.com/affaan-m/ECC) | Широкий набор skills, Windows installer, config preservation | Донор отдельных opt-in предметных skills; весь plugin с hooks/MCP не подключён |
| [GSD Core](https://github.com/open-gsd/gsd-core) | Milestones, phase execution, persistent project state | Опциональный проектный процесс; не обязательный слой для исследования/документов |
| [Spec Kit](https://github.com/github/spec-kit) | Готовые процессы, extensions, skills для Codex | Выбрать при задаче на спецификацию/разработку; не запускать параллельно второй обязательный планировщик |
| [OpenSpec](https://github.com/Fission-AI/OpenSpec) | Изменения требований, сценарии и архив решений | Альтернатива Spec Kit для проектного change workflow |
| [Worktrunk](https://github.com/max-sixty/worktrunk) | Управление worktrees вокруг текущего CLI | Полезен при нехватке штатного менеджера; здесь используются native managed worktrees |
| [Beads](https://github.com/gastownhall/beads) | Граф задач, ready queue, долговременная память | Имеет смысл для большого backlog; наш ближайший план остаётся локальным и ограниченным |
| [Serena](https://github.com/oraios/serena) | LSP/symbol tools для больших codebases, Codex CLI/App | Подключать под нужный язык; проверить license и language servers |
| [Playwright CLI](https://github.com/microsoft/playwright-cli) | Browser CLI + skills без нового LLM engine | Подключать при браузерной задаче, если текущих tools недостаточно |

Готовые решения существуют. Наш выбор — native Codex для исполнения, небольшое
детерминированное ядро для состояния/плана/приёмки и opt-in внешние modules.
Это позволяет улучшать постановку, briefs, зависимые задачи и repair без
дублирования runtime и учётных записей. Перенятые принципы реализованы своим кодом;
исходники сторонних пакетов в этот репозиторий не скопированы.

## Существенные различия текущих версий

- Старый `affaan-m/everything-claude-code` перенаправляет в ECC. Codex hooks
  требуют отдельного trust; default Chrome DevTools MCP и пример security
  settings нельзя считать нашими настройками. [Codex guide](https://github.com/affaan-m/ECC/blob/b38be6a7f6350964d59328b52f0624e1d1d552d1/.codex-plugin/README.md).
- Исторический GSD archived и направляет в GSD Core. Для исследованной default branch
  `next` совпадение с опубликованным release не проверено. Docs и package расходятся по
  минимальному Node; emitter/install надо проверять на выбранном pin.
  [package.json](https://github.com/open-gsd/gsd-core/blob/1ec5c812b79dc1cc487d0873d18b03449b11c106/package.json).
- Superpowers native Codex manifest намеренно не включает Claude hooks.
  Нельзя обещать полную Claude SessionStart parity.
  [Manifest](https://github.com/obra/superpowers/blob/2ce8aa15d05bd57213e6f746f953aafb7ded7a8b/.codex-plugin/plugin.json).
- CAO — полноценный отдельный supervisor, но Codex provider по умолчанию
  добавляет `--yolo`. Он не принят как default в текущий harness.
  [Provider docs](https://github.com/awslabs/cli-agent-orchestrator/blob/2731179845f9bd332a22e72fa44cc55345561c99/docs/codex-cli.md).
- `openai/skills` deprecated; актуальные примеры — [openai/plugins](https://github.com/openai/plugins).
  У компонентов могут быть разные licenses; общий null SPDX не подтверждает
  единую лицензию всего каталога.

## LangGraph и Langfuse

[LangGraph](https://docs.langchain.com/oss/python/langgraph/overview) полезен для
отдельного stateful/headless приложения. Его persistence и interrupts не дают
native Codex spawn/resume API. Для текущего небольшого DAG второй runtime не
нужен; библиотека не добавлена как обязательная зависимость.

[Langfuse](https://langfuse.com/docs/observability/overview) даёт tracing/evals,
а не запуск владельцев. Self-host требует инфраструктуры; OSS core и EE имеют
разные условия. Нет подтверждённого доступа к полным native Codex token/cost
traces. Наши неизвестные метрики остаются unknown, внешняя телеметрия не включена.

## Бесплатные интеграции и порядок применения

Есть полезные локальные MCP/CLI без отдельной LLM API: официальный GitHub MCP,
Playwright, reference filesystem/git/fetch servers и собственный read-only
connector статусов harness. Сервисы-назначения всё равно имеют свои auth,
permissions и limits. Context7 сочетает открытый клиент с hosted API; n8n,
Phoenix и части других продуктов имеют отдельные license conditions.

Порядок: реальная задача → проверка уже доступного tool → один подходящий
module → pin и просмотр конфигурации → изолированный pilot → проверка полезного
результата → адресное подключение. Массовая установка каталогов и автоматический
импорт hooks/security/model settings не выполняются. До pilot статус upstream
остаётся source-screened / runtime NOT_RUN.

## Полный реестр screening

| Группа | Репозиторий | Pin | Заявленная лицензия | Степень проверки |
|---|---|---|---|---|
| native_skills | [obra/superpowers](https://github.com/obra/superpowers) | `2ce8aa15d05b` | MIT | metadata_only_other_lane_owns_detail |
| native_skills | [affaan-m/ECC](https://github.com/affaan-m/ECC) | `b38be6a7f635` | MIT | metadata_only_other_lane_owns_detail |
| native_skills | [open-gsd/gsd-core](https://github.com/open-gsd/gsd-core) | `1ec5c812b79d` | MIT | metadata_only_other_lane_owns_detail |
| native_skills | [Aieda1l/codex-agent-harness](https://github.com/Aieda1l/codex-agent-harness) | `53e0a3a8439b` | MIT | metadata_only_other_lane_owns_detail |
| native_skills | [openai/plugins](https://github.com/openai/plugins) | `0722921d5542` | DEFERRED to owning lane; no root license endpoint | metadata_only_other_lane_owns_detail |
| native_skills | [openai/skills](https://github.com/openai/skills) | `49f948faa925` | UNKNOWN root; per-skill license requires inspection | README_and_license_screening |
| native_skills | [composio-community/awesome-codex-skills](https://github.com/composio-community/awesome-codex-skills) | `0930e1373789` | UNKNOWN root license | README_and_license_screening |
| planning_memory | [github/spec-kit](https://github.com/github/spec-kit) | `7370e592f3cd` | MIT | metadata_only_other_lane_owns_detail |
| planning_memory | [Fission-AI/OpenSpec](https://github.com/Fission-AI/OpenSpec) | `27c0515724de` | MIT | metadata_only_other_lane_owns_detail |
| planning_memory | [gastownhall/beads](https://github.com/gastownhall/beads) | `4a92834d575f` | MIT | focused_source_review_NOT_RUNTIME |
| planning_memory | [eyaltoledano/claude-task-master](https://github.com/eyaltoledano/claude-task-master) | `c0c98d367c55` | MIT + Commons Clause | README_and_license_screening |
| planning_memory | [OthmanAdi/planning-with-files](https://github.com/OthmanAdi/planning-with-files) | `b6580cd24af1` | MIT | README_and_license_screening |
| planning_memory | [mem0ai/mem0](https://github.com/mem0ai/mem0) | `753496cf1435` | Apache-2.0 | README_and_license_screening |
| agent_engines | [awslabs/cli-agent-orchestrator](https://github.com/awslabs/cli-agent-orchestrator) | `2731179845f9` | Apache-2.0 | focused_source_review_NOT_RUNTIME |
| agent_engines | [langchain-ai/langgraph](https://github.com/langchain-ai/langgraph) | `e5c961d7fbe7` | MIT | README_and_license_screening |
| agent_engines | [langchain-ai/deepagents](https://github.com/langchain-ai/deepagents) | `e21ce33d4486` | MIT | README_and_license_screening |
| agent_engines | [microsoft/autogen](https://github.com/microsoft/autogen) | `d07c667a3239` | MIT code (LICENSE-CODE); CC-BY-4.0 docs | README_and_license_screening |
| agent_engines | [crewAIInc/crewAI](https://github.com/crewAIInc/crewAI) | `97bbfb648117` | MIT | README_and_license_screening |
| agent_engines | [openai/openai-agents-python](https://github.com/openai/openai-agents-python) | `e355d69152d4` | MIT | README_and_license_screening |
| multi_agent_ux | [max-sixty/worktrunk](https://github.com/max-sixty/worktrunk) | `2c04fb370427` | MIT OR Apache-2.0 (README/Cargo.toml) | focused_source_review_NOT_RUNTIME |
| multi_agent_ux | [BloopAI/vibe-kanban](https://github.com/BloopAI/vibe-kanban) | `7c3807084daa` | Apache-2.0 | README_and_license_screening |
| multi_agent_ux | [generalaction/emdash](https://github.com/generalaction/emdash) | `ab766e8fbb43` | Apache-2.0 | README_and_license_screening |
| mcp_servers | [modelcontextprotocol/servers](https://github.com/modelcontextprotocol/servers) | `194cc86e3ae6` | Apache-2.0 transition; legacy MIT; docs CC-BY-4.0 | README_and_license_screening |
| mcp_servers | [github/github-mcp-server](https://github.com/github/github-mcp-server) | `f2a07cd13243` | MIT | README_and_license_screening |
| mcp_servers | [microsoft/playwright-mcp](https://github.com/microsoft/playwright-mcp) | `f6807605220f` | Apache-2.0 | README_and_license_screening |
| mcp_servers | [oraios/serena](https://github.com/oraios/serena) | `ede329aaf0c8` | GPL-3.0-or-later application; MIT SolidLSP | focused_source_review_NOT_RUNTIME |
| mcp_servers | [PrefectHQ/fastmcp](https://github.com/PrefectHQ/fastmcp) | `ffccd325f99b` | Apache-2.0 | README_and_license_screening |
| mcp_servers | [upstash/context7](https://github.com/upstash/context7) | `7836c5839183` | MIT | README_and_license_screening |
| mcp_servers | [sooperset/mcp-atlassian](https://github.com/sooperset/mcp-atlassian) | `0a5d2427144d` | MIT | README_and_license_screening |
| connector_workflows | [n8n-io/n8n](https://github.com/n8n-io/n8n) | `a98742465dfd` | Sustainable Use License + enterprise exceptions | README_and_license_screening |
| connector_workflows | [activepieces/activepieces](https://github.com/activepieces/activepieces) | `2c28ed894362` | MIT core; ee separately licensed | README_and_license_screening |
| observability_evals | [langfuse/langfuse](https://github.com/langfuse/langfuse) | `a774378582a9` | MIT core; ee separately licensed | README_and_license_screening |
| observability_evals | [Arize-ai/phoenix](https://github.com/Arize-ai/phoenix) | `0015ecea2b41` | Elastic-2.0 | README_and_license_screening |
| observability_evals | [promptfoo/promptfoo](https://github.com/promptfoo/promptfoo) | `5582f347bbc5` | MIT | README_and_license_screening |
| observability_evals | [confident-ai/deepeval](https://github.com/confident-ai/deepeval) | `90e230cbe916` | Apache-2.0 | README_and_license_screening |
| agent_engines | [microsoft/agent-framework](https://github.com/microsoft/agent-framework) | `b7884ad88eb8` | MIT | README_and_license_screening |
| mcp_servers | [microsoft/playwright-cli](https://github.com/microsoft/playwright-cli) | `faf6bc58769f` | Apache-2.0 | README_and_license_screening |

Исходные installation/runtime/Windows/WSL проверки этих upstream не запускались.
Публикуемый реестр не содержит private paths, auth, исходных transcript или raw logs.
