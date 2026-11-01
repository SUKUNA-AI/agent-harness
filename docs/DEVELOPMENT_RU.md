# Разработка 1.5.0

Сохраняйте один writer на checkout, отдельные worktrees для независимых owners
и одного интегратора. До новой функциональности фиксируйте baseline и точные
criteria. Семантический source review не заменяет runtime tests.

## Границы модулей

| Модуль | Ответственность |
| --- | --- |
| `work_contract.py`, `scheduling.py` | Closed schema, semantic plan validation, finite DAG |
| `supervisor.py`, `managed_execution.py` | FSM и orchestration; модель редактирует до freeze |
| `codex_execution.py`, `agent_lifecycle.py` | argv, exact session, process containment, global leases, worktrees |
| `strict_yaml.py`, `supervisor_store.py` | Bounded parser, schemas, atomic files, journal/replay |
| `execution_ledger.py`, `memory.py` | Provenance, validity, typed recall, rebuildable indexes |
| `input_closure.py`, `check_cache.py`, `python_runtime.py` | Bounded physical proof и narrow Windows startup/runtime closure; unsupported → miss |
| `context_reducer.py`, `format_bench.py` | Model packets и measured optional serialization |
| `core.py`, `runner.py` | Существующий snapshot/check/evidence/gate остаётся authoritative |

Имена модулей относятся к `src/agent_harness/`. Схемы в корневом `schemas/`
authoritative; wheel build копирует их в package resources. Vendored parser
имеет immutable manifest/license и проверяется без site packages.

## Документация и диаграммы

Author использует `harness-doc-author`, независимый assessor —
`harness-doc-review`; `technical_writing_level: 50` является внутренней шкалой,
не ASD-STE100 compliance. Сверяйте CLI с `--help`, примеры — с schema/normalizer,
а ограничения — с фактическими отрицательными сценариями.

Диаграммы находятся в [docs/diagrams](diagrams/README.md). При изменении flow
обновите editable `.drawio`, его SVG через draw.io Desktop и соответствующий
Mermaid block. Просмотрите экспорт и сохраните version/command/hashes в private
evidence. Наличие XML или exit0 экспортера не заменяет visual review. Для чтения
документации Node/draw.io не требуются; renderer — optional author tool.

## Проверки

```text
python tools/run_tests.py
python tools/release_check.py
cargo test --release --manifest-path native/Cargo.toml --locked
python tools/build_native.py
python tools/check_native.py
```

Сценарии используют настоящие isolated fake-Codex processes: initial edit,
точный repair, partial stream, permission denial, timeout, orphan child,
конкурирующие resumes, stale source/evidence, registry/deadline gap и managed
integration. Изолированные homes проверяют installer conflicts/reinstall/rollback.
Real Codex smoke — отдельное ограниченное наблюдение, не обычная offline CI
зависимость. Windows и Linux receipts не взаимозаменяемы.

Сначала выполните затронутые сценарии, затем полный integration gate на frozen
candidate. Изменение source, schema, verifier или toolchain делает прежнюю
приёмку неприменимой. Unknown/skipped/timeout не являются passed case.

Для релиза используйте `tools/build_release.py` на чистом committed candidate;
builder проверяет native identity и extracted distribution. Политика угроз:
[Security](SECURITY_RU.md), формат и migration:
[State](STATE_FORMATS_AND_MIGRATION_RU.md).
