# Выбранные механизмы без дополнительного runtime

Актуальная карта реализации в **1.5.0**. Pinned источники исследования сохранены
в provenance; текущий интерфейс описан в [CLI reference](CLI_REFERENCE_RU.md),
а взаимодействие компонентов — в [архитектуре](ARCHITECTURE_RU.md).

Native Codex исполняет задачу. Python stdlib хранит файлы/контракты, проверяет
согласованность и evidence. Общий bootstrap остаётся коротким: детали загружаются
по задаче через skills/workflows. Модели/provider/effort и порог 500000 сохраняются.

## Что реализовано

| Основание | Механизм в harness | Граница |
|---|---|---|
| Aider RepoMap | Rust/Tree-sitter definitions/references, граф файлов, task ranking и byte budget | Реальный локальный `context map`; эвристические links, не semantic references |
| Serena / SolidLSP | `code symbols/definition/references`, capture/requery targets, preview/apply rename | Собственный stdio LSP adapter, внешний установленный language server; сервер Serena и БД не требуются |
| Anthropic CWC / long-running agents | Неуспех до evidence, независимый evaluator, handoff | Workflow и существующий deterministic gate; не скопированный Bash hook |
| Playwright | [Browser acceptance](../workflows/browser.md), strict required JUnit cases, runtime/visual evidence | Доступный tool или установленный проектный пакет; нет глобальной автоустановки |
| Superpowers | Проверяемая гипотеза, контрпример, review packet, одна консолидированная repair-подборка | Приёмка не прерывается с критичным дефектом ради лимита review waves |
| Pi / Deep Agents | Компактный handoff, references к файлам, offload длинных outputs | Native compaction сохраняется; второй summarizer не запускается |
| mini-SWE-agent | Простой наблюдаемый цикл, явные outcomes, конечные retries/deadline | Не второй агентский движок и не unlimited loop |
| GEPA/gskill / autoresearch | [Offline experiment](../workflows/experiments.md), фиксированный evaluator, development/holdout, keep/discard | Процедура и шаблон; paid campaigns не запускаются автоматически |
| Karpathy / community skills | Явные допущения, минимальное изменение, проверяемый результат, технический вывод | Community skills не выдаются за официальный пакет Карпати |
| LLM Wiki | [Project-local Markdown](../workflows/context.md), index/decisions/log, sources и invalidation | Нет БД/embeddings; память разных проектов не объединяется |
| LLM Council | Опциональные независимые read-only оценки сложного решения | Общий бюджет native threads; голосование не доказательство |
| Rendergit | `context pack` / `context verify`, явный список файлов, hashes и limits | Нет полного repo dump; omitted/stale видны явно |

Это самостоятельная реализация механизмов. Исходный код upstream не vendored.
Рабочая навигация и rename уже включены в [LSP adapter](LSP_RU.md), поэтому
отсутствие vendored Serena не означает отсутствие этой возможности.
Для LLM Wiki gist и llm-council в исследованном snapshot не было однозначной
лицензии на копирование; используются идеи, а не их source/prompt. Pinned основания
и даты находятся в [реестре](../provenance/selected-patterns.json).

## Пример передачи на ревью

Root выбирает цель и точные файлы через поиск, формирует private source packet,
прикладывает diff, критерии и raw evidence по [шаблону](../templates/review-packet.md).
Reviewer проверяет достаточность пакета и актуальность references. Если существенный
файл omitted, он запрашивает/читает нужный раздел; `complete=false` нельзя скрывать.
Результат — подтверждённые дефекты с воспроизведением или отсутствие находок с
границами проверки. Root консолидирует repair и принимает итоговый candidate.

Wiki и handoff создаются по потребности в разрешённой области пользователя.
Файлы-шаблоны в репозитории не содержат реальную проектную память. Context packet
не служит sandbox или acceptance gate; неизвестные secrets могут не попасть под
ограниченный pattern scan, поэтому состав просматривается перед внешней передачей.

## Что не подключается

Promptfoo, NVIDIA SkillEvaluator и Harbor не входят в dependencies и текущую
процедуру. Исторические записи исследования в provenance не означают интеграцию.
Нет БД, vector store, фонового orchestrator, автоматического изменения global
policy/evaluator, автокоммита всех файлов или обещания продолжать после закрытия UI.

## Производительность и выбор языка

Измеряй отдельно native/model wait, tool calls, review/repair и локальный Python.
`python tools/profile_core.py --files 100 --checks 3 --repeats 5` исполняет изолированный
fixture с реальными subprocess checks и gate. Команда не вызывает модели/сеть и
не меняет пользовательский checkout. Snapshot time включает Git time: их нельзя
складывать. Parent CPU не включает дочерние процессы.

В 1.4 scanner/snapshot перенесён в Rust: обход, metadata, SHA-256 и manifest.
Там же выполняется RepoMap с Tree-sitter и ранжированием графа файлов. Python
сохраняет contracts, runner/gate, installer и файловый handoff. Exact manifests
сопоставляются в `tools/check_native.py`; старые evidence/schema не мигрируют.
Готовые бинарники убирают compiler у пользователя; source build требует Rust и
C toolchain для grammars. БД и постоянный индекс не нужны. Язык этого слоя сам
по себе не меняет модельную задержку. Команды и границы —
[Rust и контекст](CONTEXT_AND_NATIVE_RU.md).

Основания: [native Codex](https://github.com/openai/codex/discussions/1174),
[Rust installation](https://doc.rust-lang.org/book/ch01-01-installation.html),
[Python hashing](https://docs.python.org/3.12/library/hashlib.html).
