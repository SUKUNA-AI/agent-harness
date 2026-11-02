# Автоматическая координация native Codex — 1.5.1

Ограниченную самостоятельную задачу root выполняет напрямую. Полезные независимые части автоматически делегируются, обычно двум owners; третий нужен при независимой части и ёмкости. Последовательный план может иметь одного owner. Неделимая работа, меньшая ёмкость или недоступный native tool дают честный последовательный путь без повторного согласования уже разрешённой работы.

В native workflow пользователь не заполняет JSON. Root готовит контракт и
добавочный план по [схеме](../schemas/orchestration.schema.json), выбирает skills
и управляет native tools. Команды `plan` только проверяют и рекомендуют.
Отдельный `work` namespace 1.5.0 запускает штатный Codex, валидирует structured
plan, управляет глобальными model slots и изолированными worktrees; он использует
существующий acceptance gate. Эти два режима не объединяют рекурсивные деревья.
Полный managed lifecycle: [Dynamic agents](DYNAMIC_AGENTS_RU.md).

Флаги/exit codes собраны в [CLI reference](CLI_REFERENCE_RU.md#planning-helpers);
рабочий managed run и различие freshness/recovery — в [эксплуатации](OPERATIONS.md).

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
  "common_context": [{"path": "<absolute authorized CONTEXT.md>", "sha256": "<actual file hash>"}],
  "target_owners": 2,
  "nodes": [
    {"id": "api", "role": "owner", "goal": "Implement authorized API change",
     "cwd": "<absolute isolated worktree>", "permissions": ["read", "write", "test"],
     "write_paths": ["src/api/**"], "depends_on": [], "checks": ["unit"],
     "read_sections": [{"path": "<absolute authorized CONTEXT.md>", "sha256": "<actual file hash>", "sections": ["Goal", "API acceptance"]}],
     "outputs": [{"path": "<absolute worktree>/src/api/change.py", "description": "Verified API change with evidence"}]}
  ]
}
```

Это шаблон, не валидный готовый контракт: root подставляет реальные paths/digests/check IDs. `target_owners` — от 1 до 3, default 2; это верхняя граница полезной текущей волны, а не требование заполнить все слоты. Ноды упорядочиваются по id. Root является единственным интегратором. Нет двух writers на checkout: сравниваются физические абсолютные targets, вложенные cwd и общий Git checkout (также соседние подкаталоги одного checkout). Та же канонизация применяется к native inventory и неизвестным writers. Windows case/slash aliases сравниваются консервативно; исходный spelling сохраняется при проверке filesystem symlinks/junctions. Claims допускают только точный путь, `**` или `directory/**`; symlink writes отклоняются. Project protected paths сохраняются, включая node cwd внутри parent cwd. Проверки кооперативны и не являются OS sandbox.

У node есть `state` (по умолчанию planned), `native_ref` для follow-up, `depends_on` и `checks` из текущего контракта. Права — подмножество контракта. Review/diagnostics/plan/research/answer и reviewer не получают product write claims; разрешённые отчёты оформляются отдельным artifact contract.

Обязательные `common_context` и `read_sections` содержат существующие абсолютные local file refs с актуальным `sha256`; `read_sections.sections` задаёт непустые названия разделов/интервалы для чтения. Файл должен находиться в назначенном workspace/node cwd или быть явно объявленным `contract.inputs` (так можно подключить разрешённый private CONTEXT). Произвольный внешний project path отклоняется. У node обязательны concrete `outputs`: непустые descriptions и абсолютные paths внутри его cwd; будущие файлы могут ещё отсутствовать. Бриф и repair-бриф включают эти сведения автоматически. Root по-прежнему оценивает смысл/конфиденциальность ссылок и достаточность outputs.

Зависимость выпускается только при state accepted/integrated и `accepted_result: {run_dir, verdict_digest}` плюс `expected_child: {run_id, task_id, attempt_id, contract_digest}`. У frozen child contract обязательны `lane_id=node.id` и дополнительный `orchestration_binding`: `{node_id, parent_run_id, parent_task_id, parent_attempt_id, parent_contract_digest, plan_revision}` с точными значениями текущего parent contract/plan. Бриф печатает требуемую linkage. После новой parent attempt/revision старое принятие не используется молча: потребуется соответствующий child contract/attempt и новая проверка. Чужая accepted task также не подходит.

Читается реальный Run: проверяются snapshot, accepted verdict hash/identity, toolchain manifest hash, required results с исходными receipt digests и все declared artifact/log hashes, размеры и отсутствие посторонних artifacts. Эти проверки общие с gate; read-only helper также проверяет принятые зависимости child. Удалённый stdout/stderr/result, изменённый log или toolchain не освобождает downstream node при неизменном source. Сверяется exact set check receipts: уже принятый optional result также не может исчезнуть, а optional, изначально отсутствовавший, не становится required. Helper не запускает проверки, не создаёт gate и не меняет run; snapshot может использовать штатные read-only Git queries. Текст `passed` в плане или ready_for_review не освобождает dependency. Проверка не заменяет gates downstream candidate, не криптографически удостоверяет автора и не защищает от редактора с теми же OS правами.

## Исполняемый пример структурного плана

Сначала выполните [локальный managed fixture](OPERATIONS.md#полный-локальный-пример).
Следующий скрипт сохраните вне package как `demo_plan.py` и запустите с теми же
двумя аргументами: package root и созданный private-demo root. Он вычисляет реальные
hashes текущего contract/source, создаёт **одну** planned node и выполняет только
`plan validate`/`plan brief`. Ни agent, ни child run, ни accepted dependency не
создаются. Node output обозначает будущую работу; существование output не означает
выполнение node. Для следующей волны потребуется отдельный actual native inventory.

```python
from pathlib import Path
import json
import subprocess
import sys

package = Path(sys.argv[1]).resolve(strict=True)
base = Path(sys.argv[2]).resolve(strict=True)
sys.path.insert(0, str(package / "src"))
from agent_harness.core import digest, file_hash, snapshot

private = base / "private"
contract_path = private / "contract.json"
contract = json.loads(contract_path.read_text(encoding="utf-8"))
source = Path(contract["cwd"])
reading = source / "calc.py"
reference = {"path": str(reading), "sha256": file_hash(reading)}
plan = {
    "schema_version": 1, "plan_revision": 1,
    "contract_digest": digest(contract), "input_digest": snapshot(contract)["digest"],
    "target_owners": 1, "fallback_reason": "indivisible",
    "common_context": [reference],
    "nodes": [{
        "id": "calc", "role": "owner", "state": "planned",
        "goal": "Inspect and update only the isolated calc fixture when authorized",
        "cwd": str(source), "permissions": ["read", "write", "test"],
        "write_paths": ["calc.py"], "depends_on": [],
        "checks": ["unit", "source-observation"],
        "read_sections": [dict(reference, sections=["Whole calc.py fixture"])],
        "outputs": [{"path": str(source / "calc.py"),
                     "description": "Scoped calc implementation with applicable check evidence"}],
    }],
}
plan_path = private / "plan.json"
plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
for action, extra in [("validate", []), ("brief", ["--node", "calc"])]:
    command = [sys.executable, str(package / "tools" / "harness.py"), "plan", action,
               "--plan", str(plan_path), "--contract", str(contract_path), *extra]
    completed = subprocess.run(command, capture_output=True, text=True,
                               encoding="utf-8", check=True)
    value = json.loads(completed.stdout)
    (private / ("plan-" + action + ".json")).write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"plan": str(plan_path), "validated": True,
                  "native_agents_created": 0, "accepted_children": 0}, indent=2))
```

Минимальный standalone run не требует планирования; этот пример показывает лишь
формат optional helper. В делегированной задаче root назначает конкретный isolated
worktree каждому writer. Если изменились source bytes, common_context либо parent
contract/attempt, обновите input/contract digests и plan_revision осознанно, затем
повторите validate. Переписывание digests без пересмотра требований не делает старый
child result текущим.

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

Это формат записи наблюдения, а не готовый достоверный inventory. Подставляются
реальные refs/status/cap/source из native tool; fixture identifiers из документа
нельзя выдавать за фактически существующие threads.

Снимок годен максимум 120 секунд и требует явного boolean `budget.root_counts`, cap/source в контракте. `true` означает общий лимит с root; `false` — лимит только потомков. Для canonical `agents.max_concurrent_threads_per_session` Codex лимит относится к дочерним потокам, поэтому используется `false` ([официальный subagents guide](https://learn.chatgpt.com/docs/agent-configuration/subagents), [config reference](https://learn.chatgpt.com/docs/config-file/config-reference)). Это не позволяет переносить child-only семантику на любой runtime/account cap: для другого backend её сверяют по фактическому inventory и документации. Кап не превышает текущий наблюдаемый/разрешённый cap контракта. Root указан ровно один раз для идентификации, но считается в cap только при `root_counts=true`. Любой открытый потомок, даже неизвестный плану, считается; idle/done/stopped/unavailable занимают слот. Closed требует `closed_confirmed=true` и `closure_evidence` реального backend. Duplicate native_ref, over-cap и concurrent writer checkout конфликт отклоняются.

У `next` planned node без native_ref — новый spawn; с известным idle/done/stopped ref — follow-up уже открытого потока, без освобождения слота. Пользовательский backend может не поддерживать повтор stopped; root сверяет фактическую семантику и использует доступный ref, не выдумывает close. Неизвестный/unavailable ref требует reconciliation. `next` не переименовывает и не закрывает потоки; новая lease не должна пересекаться с прежней.

## Repair и recovery

Repair packet содержит `node_id`, текущую `plan_revision`, прежний `native_ref`, текущие `contract_digest`, `candidate_digest` (равен current input snapshot), `attempt_id`, существующий `check_id`, `status=failed|timeout|unknown`, `log` и `expected`. CLI сверяет привязку и рендерит brief с common context/read sections/outputs; реальность логов и gates проверяются существующим evidence runner/root, а не этим renderer. Root передаёт фактический лог/exit и безопасно обновляет isolated checkout к current integration candidate: текстовый follow-up файлы не меняет. Dirty work сохраняется. Две одинаковые ошибки без новой гипотезы требуют диагностики, replacement thread не сбрасывает бюджет.

Для recovery используются существующие checkpoint/resume плюс свежее наблюдение actual files/processes/native refs/неизвестных операций. Живой процесс не дублируется; неизвестный external outcome сначала проверяется. Это восстановление контекста, а не обещание native resume или работы после закрытия приложения. Owner отдаёт ready_for_review с evidence; root проверяет итог без конкурентных writes и принимает существующим gate.

## Зависимости и ограничения

LangGraph и Langfuse не нужны для этого ограниченного control plane. LangGraph даёт собственный graph runtime/checkpoints, но не штатное управление Codex threads ([официальная документация](https://docs.langchain.com/oss/python/langgraph/overview)); повтор nodes при interrupts требует идемпотентных эффектов ([interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)). Langfuse — observability, self-host требует web/worker и хранилища ([архитектура](https://langfuse.com/self-hosting)); MIT OSS имеет отдельные EE исключения ([license](https://github.com/langfuse/langfuse/blob/main/LICENSE)). Проверено 2026-10-31; зависимости не устанавливались. Нет новых accounts, SaaS endpoint, telemetry export или второго LLM loop.

Unit/integration fixtures подтверждают локальный алгоритм. Native делегация, реальные backend lifecycle semantics, восстановление после закрытия приложения и ускорение требуют отдельных наблюдаемых runtime сценариев; этот модуль сам их не доказывает. External permissions/connector availability не создаются планом и не расширяются.
