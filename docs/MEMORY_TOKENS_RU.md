# Claude-Mem и TokenEconomizer для нашего Codex

Оба пользовательских репозитория просмотрены по текущим исходникам 31.10.2026.
Оба поддерживают native Codex и объявляют Apache-2.0. Здесь они изучены по
source; их installer, hooks, модели, история и сервисы не запускались.
Полные pins и исходники — [реестр](../provenance/upstreams.json).

| Проект | Исследованный pin | Решение |
|---|---|---|
| [Claude-Mem](https://github.com/thedotmack/claude-mem) v15.7.0 | `70aa40042cfb8157a0d84954b9c0d2b30b50af80` | Кандидат для отдельной project memory; автоматически не включён |
| [TokenEconomizer](https://github.com/microsoft/TokenEconomizer) v0.1.0 | `e428fc91b0f8ffecb5a596d56b14fe2f1485a5f9` | Кандидат для локального анализа; подготовлен профиль propose/off без auto-prune |

## Claude-Mem: полезная память с отдельным observer

Есть настоящий [Codex plugin](https://github.com/thedotmack/claude-mem/blob/70aa40042cfb8157a0d84954b9c0d2b30b50af80/.codex-plugin/plugin.json)
и lifecycle hooks. События сессии превращаются моделью в observations/summaries,
записываются в локальное хранилище и затем извлекаются индексом и выбранными
подробностями. Это полезный способ не повторять контекст. Модельные summaries
остаются производными; они не заменяют исходные данные, решения пользователя и
проверенные evidence.

[Codex observer](https://github.com/thedotmack/claude-mem/blob/70aa40042cfb8157a0d84954b9c0d2b30b50af80/docs/codex-provider.md)
работает через app-server и текущую ChatGPT subscription с file-backed auth;
API-key login для этого пути не принимается. Он использует дополнительные
модельные вызовы и собственный pool, не согласованный автоматически с ledger
нашего harness. Локальное хранение не означает, что текст не передаётся выбранному
model provider. Нельзя считать observer бесплатной отдельной квотой.

Текущие defaults требуют осознанного выбора: [telemetry](https://github.com/thedotmack/claude-mem/blob/70aa40042cfb8157a0d84954b9c0d2b30b50af80/docs/public/telemetry.mdx)
opt-out, а [redaction](https://github.com/thedotmack/claude-mem/blob/70aa40042cfb8157a0d84954b9c0d2b30b50af80/docs/public/usage/auto-redaction.mdx)
выключена. Отдельный data directory и current-project context уменьшают смешение
источников; общий DB или project filter сами по себе не создают security boundary.
Windows hooks/installer должны быть проверены на фактическом клиенте.

Для первого изолированного пилота подходят такие **поддержанные переключатели**:

| Где | Настройка |
|---|---|
| Environment до worker start | `CLAUDE_MEM_TELEMETRY=0`, `CLAUDE_MEM_TELEMETRY_ERRORS=0`, `DO_NOT_TRACK=1`, отдельный `CLAUDE_MEM_DATA_DIR` |
| Project-private settings | `CLAUDE_MEM_REDACT_ENABLED="true"`, `CLAUDE_MEM_CONTEXT_PROJECTS="current"`, `CLAUDE_MEM_SESSION_START_INCLUDE_ALL_SOURCES="false"` |
| История | `CLAUDE_MEM_TRANSCRIPTS_ENABLED="false"`, `CLAUDE_MEM_CODEX_TRANSCRIPT_INGESTION="false"`, `CLAUDE_MEM_CODEX_SUBAGENT_INGESTION="false"` |
| Observer | `CLAUDE_MEM_PROVIDER="codex"`, `CLAUDE_MEM_CODEX_MAX_CONCURRENT_AGENTS="1"` |
| Модель observer | `CLAUDE_MEM_CODEX_MODEL` и `CLAUDE_MEM_CODEX_REASONING_EFFORT` назначаются явно по выбору пользователя; чужой default effort не принимается молча |
| Хранение/сеть | `CLAUDE_MEM_CHROMA_ENABLED="false"`, `CLAUDE_MEM_WORKER_HOST="127.0.0.1"` |

Имена/типы проверены в [SettingsDefaultsManager](https://github.com/thedotmack/claude-mem/blob/70aa40042cfb8157a0d84954b9c0d2b30b50af80/src/shared/SettingsDefaultsManager.ts)
и provider guide. Это кандидат комбинации, не проверенная совместная конфигурация.
Knob подчинения observer нашему native budget не обнаружен; pool=1 не решает
учёт автоматически. До его решения глобальное включение не выбрано.

## TokenEconomizer: анализ с обратимыми изменениями

Native plugin использует Python stdlib и текущий coding agent. Его
[defaults](https://github.com/microsoft/TokenEconomizer/blob/e428fc91b0f8ffecb5a596d56b14fe2f1485a5f9/tokeneconomizer/plugin/config.py)
включают auto tool pruning и loopguard nudge; optional distiller/judge добавляют
модельные sidecalls той же подписки/API. Пакет не увеличивает её лимит.

Особенно существенен [Codex pruning writer](https://github.com/microsoft/TokenEconomizer/blob/e428fc91b0f8ffecb5a596d56b14fe2f1485a5f9/tokeneconomizer/plugin/denylist.py):
он может выключать collaboration, goals и image_media в project config. Это
расходится с нашим default multi-agent UX. В просмотренной [Codex fold branch](https://github.com/microsoft/TokenEconomizer/blob/e428fc91b0f8ffecb5a596d56b14fe2f1485a5f9/tokeneconomizer/plugin/fold.py)
generic `toolprune_protect` не обеспечивает подтверждённый фильтр этих packages.
Такую защиту здесь не обещаем.

Подготовлен [пример environment](../profiles/token-economizer.env.example):

```text
TE_MICROSKILLS=off
TE_TOOLPRUNE=propose
TE_LOOPGUARD=off
```

Документированная команда установленного upstream skill:
`$tokeneconomizer:config set toolprune=propose microskills=off loopguard=off`.
Она здесь **не выполнялась**. Режим propose сохраняет рекомендации без нового
apply. Для ранее убранных entries нужен `$tokeneconomizer:toolprune restore`:
простое off не восстанавливает конфигурацию. `TOKENECONOMIZER_HOME` позволяет
отделить private state; same-origin worktrees не считаются границей PUBLIC/PRIVATE.
Основания: [plugin design](https://github.com/microsoft/TokenEconomizer/blob/e428fc91b0f8ffecb5a596d56b14fe2f1485a5f9/docs/PLUGIN.md)
и [state identity](https://github.com/microsoft/TokenEconomizer/blob/e428fc91b0f8ffecb5a596d56b14fe2f1485a5f9/tokeneconomizer/plugin/paths.py).

Публичные проценты экономии относятся к отдельному benchmark. Сам plugin design
различает benchmark intervention и live hooks. В нашем harness экономия не
измерена; missing/fallback model pricing не становится точной стоимостью или
метрикой остатка подписки. Полезность проверяется по времени до принятого
результата, повторным объяснениям и реальным расходам на bounded задаче.

## Что уже применяется в нашем комплекте

Общий контекст, конкретные reading sections/output в брифах, project-separated
private state, checkpoints, актуальные evidence и ограниченные metadata MCP.
Это снижает необходимость повторять порядок работы без глобального импорта
истории и дополнительных фоновых моделей. Сторонний код не скопирован.

Следующее подключение выбирается под реальную задачу: память — после определения
допустимых источников и observer budget; оптимизатор — с отключённым auto-prune
и проверкой rollback. Source screening, установка и runtime остаются отдельными
статусами. Здесь готов обзор и шаблон; оба upstream **не установлены**.
