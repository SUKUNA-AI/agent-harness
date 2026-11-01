# Native Agent Harness 1.5.0

Дата candidate: 2026-11-01. База: `5f06a2fdebe48859189f285ad107a8c2c360579e`
(1.4.2). Фактические hashes, команды, результаты и публикация приводятся в
implementation/verification report; этот документ описывает поставляемый интерфейс.

## Новые функции

- Foreground `work` supervisor: реальное редактирование через Codex exec,
  exact session resume, freeze/check/gate, bounded repair, stop и recovery.
- Strict YAML со схемами, vendored PyYAML6.0.3 MIT, атомарные checkpoints,
  hash-chained journal и явный repair только incomplete final append.
- Ledger inform/advisory govern, conservative reproducible-check cache,
  opt-in short/episodic/semantic memory с source/evidence invalidation.
- Bounded reducer, адресные raw pointers, четыре RepoMap query, selective TOON
  с измеренными A/B eligibility и automatic JSON fallback.
- Structured plan, semantic DAG/scope validators, critical path, global cap5,
  изолированные worktrees и последовательная интеграция в staging candidate.
- Два documentation skills, внутренний style50, самостоятельные guides и
  реально проверяемые Mermaid diagrams.
- Детерминированный ZIP_STORED, canonical Git blobs, source/binary hashes,
  extracted install/verify/reinstall/rollback smoke.

## Совместимость и обновление

Legacy JSON contracts, core Run receipts и installer manifests сохраняются.
Новые `work` contracts не переписывают `config.toml`. Installer изменяет свои
marker blocks/assets; сохраняет model/provider/reasoning, approvals, sandbox,
MCP и пользовательские настройки. Compaction500000 сохраняется. Native child
cap5 и global managed cap5 имеют разную семантику.

Обновление: [SETUP_RU](../SETUP_RU.md), сначала doctor/dry-run, затем apply/verify.
Rollback отменяет последнюю installer-транзакцию; архив и исходный checkout
не удаляются. Source/version mismatch найденного native bundle требует
пересборки/подходящего архива, а не обхода Python fallback.

## Измерения и границы

Исходный [baseline](BASELINE_1_5_0.md): Python393 tests, 43 платформенных пропуска,
Rust18/18 и native parity passed. Эти числа не являются финальным test count1.5.0.
Native CLI compatibility проверяется на Codex0.166.0-alpha.5.1; модель/provider
сохраняются. Python3.11+; build helper Rust1.90.0/C toolchain.

Реальный pilot JSON/Markdown/TOON: 5 repeats, 15/15 correct; paired median
input saving TOON около0.71%, sign-test p0.1875. Результат `measured_negative`:
TOON выключен. Это диагностический pilot одной таблицы, не общая оценка качества.
Сравнения больших workloads и scheduler1/2/3/5 не дают общего performance claim
без сопоставимых samples; их протокол и ограничения — [eval guide](PERFORMANCE_EVAL_RU.md).

Govern для встроенных tools Codex advisory. Отсутствие native tool inventory
proof блокирует managed concurrency>1. Token cap проверяется на turn boundaries;
внутренний hard decoding cap через CLI недоступен. После закрытия foreground
процесса нужен resume. Local config не настраивает hosted Work и не гарантирует
hot reload старого чата. Release не требует дополнительных сервисов/БД.
