# Изменения после 1.7.0 — Unreleased

Следующий выпуск готовится. Этот документ описывает изменения исходников;
он не объявляет новую версию, Git tag, release asset или успешную полную приёмку.

## Исходники и опубликованный пакет

| Объект | Закрепление и содержимое |
| --- | --- |
| Опубликованный `v1.7.0` | Source target `debe4df58f243929d67c85517a58436a5468fc9f`; неизменяемый архив и собственный manifest |
| ZIP `agent-harness-1.7.0.zip` | SHA-256 `3e573747da2a7ad777d08330781c1d3fcda2ca45a7457ee068ac45efe5497e1c`; не содержит SSE EOF/cancel fix |
| Source fix `277519e3494e5878670f57e36aba357ac990547e` | Исправление runtime post-response audit OpenCode и семь новых lifecycle regression tests |
| Текущие руководства | Описывают новый source candidate; документационный commit сам не пересобирает ZIP и не меняет установленный профиль |

Публичный архив скачивайте из [выпуска 1.7.0](https://github.com/SUKUNA-AI/agent-harness/releases/tag/v1.7.0)
и сверяйте с его `SHA256SUMS`. Git clone содержит новые исходники; готовые
native bundles и статус их release verification определяются отдельно.
Прежние [release notes 1.7.0](RELEASE_NOTES_1.7.0.md) остаются историей того архива.
Не переносите scoped проверки нового source на уже опубликованный ZIP.

## Исправление OpenCode response lifecycle

Provider SDK может прочитать terminal SSE и отменить response body до network
EOF. Старый `TransformStream.flush` тогда не запускал обязательный
post-response MCP audit: реальные model/tool calls могли состояться, но
completion оставался `UNKNOWN_OUTCOME` из-за отсутствующего `after_response`.

Новый guard ожидает успешную upstream cancellation либо clean EOF, проверяет
source и live MCP connections и пишет audit один раз. Stream error, failed
cancel или disconnect не дают успешного receipt. Native step stop/finish,
clean normalized JSONL EOF и process cleanup проверяются отдельно: audit
не превращает незавершённый turn в completed. Реализация и точная граница —
[OpenCode V2](OPENCODE_RU.md#завершение-sse-и-проверка-mcp-после-ответа).

## Что проверено

- Семь regression tests запускают настоящий guard в Node Web Streams: EOF,
  terminal cancel, connection loss, pending audit, rejected cancel, stream
  error и незавершённый native step.
- Scoped SSE/native OpenCode/tool-profile запуск: **77 tests, 76 PASS, 1 SKIP**,
  exit 0. Native permission/negative/resume проверки используют настоящий
  OpenCode 2.2.5 и isolated local scripted provider без платных model calls.
- Отдельный реальный Linux OpenRouter Hydrogen/Flex turn выполнил
  `serena_find_symbol` и `context7_resolve-library-id`. Post-response audit,
  неизменность source и завершение owned processes проверены. Статус —
  `OBSERVED_REAL_OPENROUTER_MCP`, acceptance — `not_performed`.

Старый full baseline **1370 PASS, 75 SKIP** относится к отдельному snapshot.
Он не является full-suite доказательством SSE candidate. Реальный smoke не
заменяет supervisor plan/check/review/gate или workload benchmark.
Полные receipts остаются private; в Git не переносятся host paths, auth,
session transcripts или содержимое пользовательских configs.

## Сохранённые границы

Codex остаётся default backend; model/provider/effort и global defaults не
меняются. Восемь Tool Profiles, отдельный MCP process/state на worktree,
общие managed quotas **12 sessions / 3 reviewers**, scopes, freeze и final gate
остаются прежними. OpenCode V2 2.2.5 — optional managed leaf backend;
OpenCode-native multi-agent — `NOT_IMPLEMENTED`.

Strict Codex owned transport — `EXPERIMENTAL`; платный Codex workload не
квалифицирован. Optional tool live-сценарии на Windows этим Linux evidence
не квалифицированы. Docling исключён по выбору оператора и не устанавливался.
`documents` остаётся шаблоном для отдельной квалификации.

Переносимые команды: `python tools/harness.py tools show/check/catalog/qualify/apply`
и `work start/resume` с полным contract. `tools apply` создаёт новый private
fragment, не редактирует global config и не активирует профиль автоматически.
Машинные `codex-harness`/`opencode-harness` — private operator helpers,
отсутствующие в CLI/пакете/ZIP. MCP исполняются локально; OpenRouter служит
native model provider OpenCode. Новая сессия/процесс получает изменения по
штатным правилам клиента; hot reload открытого чата не обещается.

[Эксплуатация tooling](TOOLING_OPERATIONS_RU.md) ·
[Матрица проверок](TOOLING_ACCEPTANCE_RU.md) · [CLI](CLI_REFERENCE_RU.md)
