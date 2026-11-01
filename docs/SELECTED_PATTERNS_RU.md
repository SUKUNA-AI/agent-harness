# Выбранные механизмы без дополнительного runtime

Native Codex исполняет задачу. Python stdlib хранит файлы/контракты, проверяет
согласованность и evidence. Общий bootstrap остаётся коротким: детали загружаются
по задаче через skills/workflows. Модели/provider/effort и порог 500000 сохраняются.

## Что реализовано

| Основание | Механизм в harness | Граница |
|---|---|---|
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

Полный перенос на Rust пока не нужен для correctness и не даёт обещания ускорить
модель. Сначала устраняются лишние subprocess и повторная работа. Если замеры
реальных проектов покажут существенный CPU hot path либо нужен единый portable
binary, Rust разумнее оценивать отдельным compatibility-preserving прототипом:
те же CLI/schema/receipts, golden fixtures и Windows/Linux acceptance.
C++ имеет смысл при конкретной существующей ABI/библиотеке; сейчас такой причины
не выявлено. Rust тоже требует compiler/linker для сборки из source; готовые
release binaries могут убрать toolchain у пользователя для обоих языков.

Основания: [native Codex](https://github.com/openai/codex/discussions/1174),
[Rust installation](https://doc.rust-lang.org/book/ch01-01-installation.html),
[Python hashing](https://docs.python.org/3.12/library/hashlib.html).
