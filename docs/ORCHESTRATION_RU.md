# Автоматическая координация native Codex

Для содержательной задачи root по умолчанию назначает двух полезных независимых owners; третьего — при независимой части и ёмкости. Простая задача выполняется напрямую. Неделимая работа, меньшая ёмкость или недоступный native tool дают честный последовательный путь без повторного согласования уже разрешённой работы.

Пользователь не заполняет JSON. Root готовит контракт и добавочный план по [схеме](../schemas/orchestration.schema.json), выбирает workflows/skills и сам управляет native tools. Python только проверяет план и печатает рекомендации. Он не запускает LLM, agents, Git mutation, install, внешние запросы или acceptance gate.

## Команды

Из checkout доступен `python tools/harness.py plan <action>`:

```text
plan validate --plan <plan.json> --contract <contract.json>
plan brief --plan <plan.json> --contract <contract.json> --node <id>
plan next --plan <plan.json> --contract <contract.json> --native-snapshot <observed.json>
plan repair-brief --plan <plan.json> --contract <contract.json> --node <id> --repair <failure.json>
```

Все команды только читают; результат — JSON в stdout, ошибка — JSON в stderr/exit 2. `next` с пустой волной успешно сообщает причины ожидания; это не passed и не blocker всей задачи. CLI рекомендует волну, а root перед **каждым** spawn/follow-up снова наблюдает native состояние и сериализует вызовы. Нет reservation, auto-spawn или гарантии атомарности с backend. При потерянном ответе native operation сначала наблюдают фактический исход, не повторяют вслепую.

## Контракт и план

`contract_digest = core.digest(contract)`, `input_digest = core.snapshot(contract)["digest"]`. План привязан к текущим байтам и inputs; после изменения входов root безопасно обновляет attempt/контракт по существующему процессу и выпускает новую plan revision. Contract-v1 не меняется.

```json
{
  "schema_version": 1,
  "plan_revision": 1,
  "contract_digest": "<sha256 canonical contract>",
  "input_digest": "<sha256 current snapshot>",
  "target_owners": 2,
  "nodes": [
    {"id": "api", "role": "owner", "goal": "Implement authorized API change",
     "cwd": "<absolute isolated worktree>", "permissions": ["read", "write", "test"],
     "write_paths": ["src/api/**"], "depends_on": [], "checks": ["unit"]}
  ]
}
```

Это шаблон, не валидный готовый контракт: root подставляет реальные paths/digests/check IDs. `target_owners` — 2 или 3. Ноды упорядочиваются по id. Root является единственным интегратором. Нет двух writers на checkout; совпадающие/родительские claims, Windows case/slash aliases и generated outputs сериализуются даже в разных worktrees. Claims допускают только точный путь, `**` или `directory/**`; symlink writes отклоняются. Project protected paths сохраняются. Проверки кооперативны и не являются OS sandbox.

У node есть `state` (по умолчанию planned), `native_ref` для follow-up, `depends_on` и `checks` из текущего контракта. Права — подмножество контракта. Review/diagnostics/plan/research/answer и reviewer не получают product write claims; разрешённые отчёты оформляются отдельным artifact contract.

Зависимость выпускается только при state accepted/integrated и `accepted_result: {run_dir, verdict_digest}`: читается реальный Run, проверяются актуальный snapshot, сохранённый accepted digest и хеш verdict с accepted. Текст `passed` в плане или ready_for_review не освобождает dependency. Эта проверка не заменяет gates downstream candidate, не криптографически удостоверяет автора и не защищает от редактора с теми же OS правами.

## Наблюдение native threads

Root сохраняет адаптированный **реально наблюдаемый** полный список дерева; synthetic fixtures нельзя выдавать за это наблюдение:

```json
{
  "complete": true, "source": "native list tool receipt id",
  "observed_at": "<UTC ISO timestamp with timezone>", "cap": 6, "root_ref": "root",
  "threads": [
    {"native_ref": "root", "status": "running"},
    {"native_ref": "owner-1", "status": "idle", "parent_ref": "root"}
  ]
}
```

Снимок годен максимум 120 секунд и требует `budget.root_counts=true`, cap/source в контракте. Кап не превышает текущий наблюдаемый/разрешённый cap контракта. Root указан ровно один раз. Любой открытый потомок, даже неизвестный плану, считается; idle/done/stopped/unavailable занимают слот. Closed требует `closed_confirmed=true` и `closure_evidence` реального backend. Duplicate native_ref, over-cap и concurrent writer checkout конфликт отклоняются.

У `next` planned node без native_ref — новый spawn; с известным idle/done/stopped ref — follow-up уже открытого потока, без освобождения слота. Пользовательский backend может не поддерживать повтор stopped; root сверяет фактическую семантику и использует доступный ref, не выдумывает close. Неизвестный/unavailable ref требует reconciliation. `next` не переименовывает и не закрывает потоки; новая lease не должна пересекаться с прежней.

## Repair и recovery

Repair packet содержит `node_id`, прежний `native_ref`, текущие `contract_digest`, `candidate_digest` (равен current input snapshot), `attempt_id`, существующий `check_id`, `status=failed|timeout|unknown`, `log` и `expected`. CLI сверяет привязку и рендерит brief; реальность логов и gates проверяются существующим evidence runner/root, а не этим renderer. Root передаёт фактический лог/exit и безопасно обновляет isolated checkout к current integration candidate: текстовый follow-up файлы не меняет. Dirty work сохраняется. Две одинаковые ошибки без новой гипотезы требуют диагностики, replacement thread не сбрасывает бюджет.

Для recovery используются существующие checkpoint/resume плюс свежее наблюдение actual files/processes/native refs/неизвестных операций. Живой процесс не дублируется; неизвестный external outcome сначала проверяется. Это восстановление контекста, а не обещание native resume или работы после закрытия приложения. Owner отдаёт ready_for_review с evidence; root проверяет итог без конкурентных writes и принимает существующим gate.

## Зависимости и ограничения

LangGraph и Langfuse не нужны для этого ограниченного control plane. LangGraph даёт собственный graph runtime/checkpoints, но не штатное управление Codex threads ([официальная документация](https://docs.langchain.com/oss/python/langgraph/overview)); повтор nodes при interrupts требует идемпотентных эффектов ([interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)). Langfuse — observability, self-host требует web/worker и хранилища ([архитектура](https://langfuse.com/self-hosting)); MIT OSS имеет отдельные EE исключения ([license](https://github.com/langfuse/langfuse/blob/main/LICENSE)). Проверено 2026-10-31; зависимости не устанавливались. Нет новых accounts, SaaS endpoint, telemetry export или второго LLM loop.

Unit/integration fixtures подтверждают локальный алгоритм. Native делегация, реальные backend lifecycle semantics, восстановление после закрытия приложения и ускорение требуют отдельных наблюдаемых runtime сценариев; этот модуль сам их не доказывает. External permissions/connector availability не создаются планом и не расширяются.
