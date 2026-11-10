# Источники и границы воспроизведения — 1.6.1

Дата сверки первичных metadata: **2026-11-01**. Таблица связывает идеи с
конкретными модулями. Это independent engineering implementation: наличие
ссылки не означает перенос опубликованного кода или воспроизведение paper.
Цифры качества/стоимости авторов нельзя переносить на этот релиз.

| Первичный источник | Проверенная версия/дата | Перенос в harness | Что не воспроизведено |
| --- | --- | --- | --- |
| [Ledger: Turning Interaction History into Execution State](https://arxiv.org/abs/2608.00808) | arXiv v1, 2026-08-01 | `execution_ledger.py`: inform, advisory govern, version-bound observations | SWE-bench paper experiment; глобальный hook над всеми native Codex tools; опубликованные quality/cost gains |
| [MemTrace: State-Consistent Memory](https://arxiv.org/abs/2610.04838) | arXiv v3, 2026-10-10 | `memory.py`: provenance, compact anchors, stale/superseded recall exclusion | Полный paper Memory Trace Graph и benchmark suite; DeepSWE/SWE-EVO/Milestone результаты |
| [AgentDiet: Reducing Cost of LLM Agents with Trajectory Reduction](https://arxiv.org/abs/2509.23586) | arXiv v2, 2026-03-15; metadata сообщает FSE 2026 | `context_reducer.py`: duplicates/stale/irrelevant output reduction с pointers | Вмешательство в скрытую trajectory Codex; заявленные paper token/cost percentages |
| [PRO-LONG: Programmatic Memory Enables Long-Horizon Reasoning](https://arxiv.org/abs/2607.20064) | arXiv v3, 2026-10-12 | `Ledger.show`, `Memory.show`: адресный поиск локальных observations по ID/offset | Полные NetHack/ARC-AGI-3 эксперименты, их scores и token ratios |
| [Aider pinned RepoMap](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/repomap.py) | Pinned исходник, existing implementation reference | Existing Rust `native/src/repomap.rs`: symbol/file graph и task ranking | Копирование Aider implementation, его cache/SQLite stack или semantic guarantees |
| [TOON official specification](https://github.com/toon-format/spec) | Прочитан primary README: tabular uniform arrays; narrow implementation boundary | `format_bench.py`: flat tabular encoder, measured eligibility/fallback | General-purpose TOON parser, universal compression claim и результаты чужих benchmarks |
| [OpenAI non-interactive Codex](https://developers.openai.com/codex/noninteractive) и [CLI reference](https://learn.chatgpt.com/docs/cli/reference) | Дополнительно проверяется actual installed CLI `--help` | `codex_execution.py`: `exec --json`, exact resume, structured-output planner | Private/unsupported native runtime API, hot reload текущей сессии и offline model inference |
| [StrictYAML design](https://hitchdev.com/strictyaml/) | Архитектурный ориентир restrictive YAML | `strict_yaml.py`: закрытые schemas и запрет dangerous YAML constructs | Копирование StrictYAML parser; вместо него vendored PyYAML 6.0.3 MIT с явным адаптером |
| [Serena](https://github.com/oraios/serena) | Optional integration reference; app и SolidLSP оцениваются отдельно | Existing `lsp.py`: on-demand semantic navigation; query fallback | Копирование GPL app под видом MIT; автоматическая установка Serena или обязательный server |

Для references с pinned commit Git URL фиксирует просмотренный source target.
Строки «existing reference» описывают уже присутствующий в baseline design,
а не новый независимый эксперимент этого релиза. Лицензии реально bundled
зависимостей хранятся рядом с компонентами и native bundle; эта таблица не
заменяет NOTICE/license review при добавлении нового чужого кода.

## Исследовательские идеи без runtime зависимости

[Agent Retrieval Bench](https://arxiv.org/abs/2607.24882) задаёт полезные
категории related-tests/trace-to-code/change-impact/symbol-context; здесь
реализованы bounded engineering queries и собственные fixture tests, а не
заявлено прохождение всего benchmark.

[AgentSpawn](https://arxiv.org/abs/2602.07072) используется как направление
условной декомпозиции. Локальный scheduler строится из валидированного DAG,
scope ownership, model leases и observed durations; опубликованное ускорение
не считается нашим результатом. [CWC long-running agents](https://github.com/anthropics/cwc-long-running-agents),
[Superpowers final review](https://github.com/obra/superpowers/blob/2ce8aa15d05bd57213e6f746f953aafb7ded7a8b/skills/subagent-driven-development/references/final-review.md)
и [Pi compaction](https://github.com/earendil-works/pi/blob/6291b9bcc3ee9480c2caba8676fc2f6cd321eaa9/packages/coding-agent/docs/compaction.md)
служат procedure references для independent acceptance, consolidated repair и
handoff. Их полный runtime и experimental outcomes не воспроизводились.

GEPA/Deep Agents и другие перечисленные в design материалы не являются
обязательными dependencies. XGrammar/Outlines constraints для управляемого
inference backend не заменяют Codex structured output или semantic/security
validation; отдельный LLM backend в этом пакете не введён.

Документационные `.drawio`/SVG созданы в этом проекте. Публичная галерея содержит
собственные editable XML и эквивалентные самостоятельные SVG; Mermaid хранит
тот же граф в руководстве. Для экспорта можно использовать
[официальный draw.io Desktop](https://github.com/jgraph/drawio-desktop) либо
подготовить native vector SVG из той же модели. Фактический renderer и visual
review фиксируются отдельно. Runtime редактора и renderer не входят в harness
и не становятся обязательной dependency CLI или Codex profile.

## Наши проверки

Проверки связываются с текущим Git SHA, native binary и фактически
наблюдённой платформой в CI и локальных receipts. Тесты
`test_knowledge.py`, `test_supervisor_store.py` и supervisor/managed suites
проверяют local functionality и отрицательные сценарии. Fake adapter
используется для deterministic regression; он не подтверждает model quality.

Протокол реальных A/B, definitions и selection policy находятся в
[PERFORMANCE_EVAL_RU](PERFORMANCE_EVAL_RU.md). Без сопоставимых real runs вывод
остаётся `insufficient_data`. Source citation, passed unit test и numerical
performance evidence являются разными видами подтверждения.
