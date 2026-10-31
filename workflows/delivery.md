# Реализация и исправление

1. Установи разрешённую цель, актуальные инструкции, baseline и dirty/untracked состояние; сохрани чужие изменения без reset/stash/массового add.
2. Для значимой работы создай сохраняемый контракт с digest: cwd, scope/protected objects, критерии и required checks. В shared YAML/документе разрешай конкретные records/области.
3. Для полезных независимых частей создай native owners в общей ёмкости; один writer на checkout, свои worktrees и ветки от зафиксированной базы. Root единолично интегрирует, generated outputs и ресурсы сериализуются.
4. Owner реализует, запускает подходящие проверки, диагностирует и исправляет в scope. При двух одинаковых провалах без новой гипотезы не повторяй вслепую. Возвращай ошибку прежнему owner с failing check/log/current candidate; создай новую attempt и безопасно обнови repair checkout.
5. Подготовь generated candidate, затем `freeze`, required `check` и содержательные/runtime/visual evidence. Нельзя менять candidate во время финального gate. `gate` root проверяет exact inputs/contract/attempt/evidence/scope; ready_for_review ещё не accepted.
6. Проверь взаимодействия и конечный пользовательский сценарий. После исправления повтори затронутые проверки и обязательные интеграционные gates. Сохрани checkpoint, результат и ограничения. Push/PR/merge/deploy требуют отдельного действующего основания.
