# Pinned upstream preparation protocol

Новые API дополняют `artifact-json-v1` конкретными import/preparation и evaluator protocol для трёх внешних suites. Они не скачивают datasets/images, не запускают модель и не открывают observed registry. Docker в текущей среде недоступен; production qualification и реальный G4 остаются `NOT_RUN`.

`benchmark_protocol.validate_prepared_profile(profile_ref, assets_root, manifest=None)` проверяет закрытый v1 profile и содержимое его refs: resources, images/preparation/toolchains/workspaces, network/model route, private state, root/child policies и отдельный cohort/evaluator для каждой внешней suite. С `manifest` требуется точное совпадение identity, policies, task IDs/source refs, dataset revision и evaluator entrypoint. `build_prepared_profile(...)` публикует такие документы эксклюзивно, не перезаписывая dataset assets. BudgetAuthorization остаётся отдельной явной authority; содержимое budget/profile файла расход не разрешает.

Cohort фиксирует полный **выбранный pinned index** и число его task refs. Labels: `selected_full_pinned_index`, `custom_selection`, `synthetic_fixture`. Валидатор проверяет membership и хэши, но не аттестует происхождение index и не доказывает, что индекс равен официальному полному dataset. Неполный или изменённый эксперимент нельзя называть официальным полным split. Нет выдуманных upstream SHA: реальные revisions, assets, toolchains и images передаются заранее pinned. В тестах используются только собственные synthetic данные.

`benchmark_importers.import_upstream_source(source_ref, assets_root, profile_ref)` строит закрытый immutable preparation plan. Source descriptor привязывает raw metadata, files, dataset/evaluator revisions, task ID, язык и (для SWE) base checkout inventory. `materialize_upstream_plan(...)` заново проверяет plan и создаёт отдельные source/evaluator каталоги и typed receipt. Частичная materialization не повторяется автоматически. `verify_upstream_materialization(...)` проверяет receipt SHA, physical bindings, исходные/immutable/protected inventories и свежий Git root.

| Suite / protocol | Preparation | Independent evaluation |
|---|---|---|
| SWE Verified / `swebench-jsonl-v1` | Raw record + проверенный local base checkout; issue идёт в solve, gold/test patches остаются private | JSONL prediction с patch из текущих source bytes; локальный dataset file и `swebench.harness.run_evaluation`; task-bound native `report.json.resolved` |
| Terminal 2.1 / `harbor-task-v1` | `instruction.md`, raw `task.toml`, environment/tests/solution inventory; source из pinned **prepared image workspace**, tests/solution private | Фиксированный `/tests/test.sh`, отдельные solve/tests/output mounts, `/logs/verifier/reward.txt` или scalar `reward.json` |
| Aider / `aider-exercism-v1` | `.meta/config.json` file sets и `.docs` instructions; solution/build inputs visible, tests/examples private | Fresh grade workspace, восстановленные original tests/examples, language-specific test command; Java disabled annotations снимаются как в upstream harness |

Эти протоколы основаны на [SWE evaluation guide](https://www.swebench.com/SWE-bench/guides/evaluation/), [его локальном dataset loader](https://github.com/SWE-bench/SWE-bench/blob/main/swebench/harness/utils.py), [Terminal 2.1 repository](https://github.com/harbor-framework/terminal-bench-2-1), [Harbor task format](https://docs.harborframework.com/tasks/overview), [verifier protocol](https://docs.harborframework.com/tasks/verifier) и [Aider benchmark implementation](https://github.com/Aider-AI/aider/blob/main/benchmark/benchmark.py). Это интерфейсы, а не pinned task assets. Конкретная evaluator revision и её entrypoint/helper bytes входят в profile; несовместимую версию нельзя незаметно подменить latest.

`capture_local_git_checkout(...)` использует только read-only local Git, проверяет HEAD и каждый blob перед pinned export. Source file descriptors сохраняют modes `100644/100755`; symlinks/submodules явно не поддержаны. Prepared solve получает новый Git root без ambient Git config/hooks/templates. Для SWE его initial tree обязан совпасть с captured original tree; новый preparation commit **не выдаётся** за оригинальный base commit. Project-local AGENTS сохраняются. Actual Codex instruction discovery boundary всё равно требует qualification.

Baseline capture требует raw-byte равенства worktree каждому immutable Git blob.
Git-clean checkout с CRLF, clean/smudge, ident или encoding transformations не
является таким baseline. CRLF при LF blob явно отклоняется с
`BENCH_BASE_CHECKOUT_FILTERED_BYTES_UNSUPPORTED`; другие несовпадения сохраняют
`BENCH_DIRTY_BASE_CHECKOUT`. Capture не запускает filters и не нормализует bytes.
Подготовьте отдельный checkout с отключёнными EOL/filter transformations и
проверьте raw bytes перед capture. Fixtures задают собственные Git config и
attributes settings, не наследуя autocrlf оператора. SWE text patch делит строки
только по LF; CR, U+2028, U+0085, VT и FF остаются содержимым строки, отсутствие
final LF передаётся Git marker. Binary/NUL patch отклоняется. Serialized relative
paths arm inventory используют POSIX `/`; reader отвергает backslash, drive,
пустые компоненты и dot/traversal. Эти controls не аттестуют Windows runtime.

Для двух собственных suites `LocalTaskBundleAdapter(fresh_git=True)` создаёт materialization **v2** с настоящим physical Git binding. Readers принимают v1/v2; v1 сохраняет прежнее поведение и запрещает добавленный `.git`. Только проверенный v2 исключает собственные Git metadata из source/oracle/milestone projection. Old v1 reader отказывает v2; сохранённые v1 receipts не повышаются автоматически.

`UpstreamBenchmarkAdapter(profile_ref, assets_root, boundary=..., arm_builder=..., evaluator_boundary=...)` реально потребляет plan, root/child policy refs и pinned arm receipt. Builder передаётся как `ArmHomeBuilder.prepare_arm(..., workspace=source)`, затем `verify_arm/launch_environment`; transport получает отдельную environment копию через `with_environment`, без изменения process-global environment. Source/arm receipts SHA входят в frozen task contract. `PortfolioBenchmarkAdapter` маршрутизирует три внешние и две собственные suites, применяя тот же per-arm builder к собственным задачам.

`independent_grade` — единственный grading callback для этих adapters. Он сохраняет исходный deadline и использует явный `UpstreamEvaluatorBoundary.run(...)`; default unavailable. Нельзя сериализовать untrusted boolean как qualification. Отдельный runtime backend должен реально обеспечить mounts, resource/image pins, agent network denial, model transport и запрет peer/evaluator доступа. Любая ошибка после grade intent остаётся unknown и не вызывает blind replay. Submission gate внутри work contract проверяет только представление candidate на private evaluation; task quality определяют upstream tests, а не этот structural gate. Unavailable metrics возвращаются с unknown coverage, не нулями.

Ограничения текущего protocol implementation: ordinary bounded UTF-8 text prediction patches для SWE (binary patch edits отклоняются); single-step Harbor task format1.0–1.4 без GPU/MCP и с заранее prepared workspace; labeled/multi-metric reward требует отдельного pinned aggregation profile и сейчас отклоняется. Aider JS/C++ helper scripts должны быть pinned, шесть языковых toolchains заранее готовы. Byte/node/file limits strict parser сохраняются; слишком большой или неподдержанный inventory явно отказывает. Полные официальные datasets, реальное выполнение этих toolchains и runtime/client/isolation qualification не проверены. Поддержка Codex вместо Aider-agent не создаёт сопоставимый Aider leaderboard score.
