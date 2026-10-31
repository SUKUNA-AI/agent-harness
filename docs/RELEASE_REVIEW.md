# Проверка поставки 1.1.0 — 31.10.2026

Общий portable harness собран из рабочего snapshot 1.0.1 и новых проверенных
изменений. Исходный checkout сохранён; в GitHub-пакет не включены его Git history,
проектные адаптеры, auth, machine overlay, приватные runs и raw receipts.
Исходные hashes — `provenance/source-snapshot.json`.

## Фактически выполнено локально

| Объект | Результат и граница |
|---|---|
| Полная интегрированная suite | **138 tests, OK, 0 skipped**, Windows / Python 3.13; `python tools/run_tests.py` |
| Orchestration repair | 31 targeted tests и 55 core regressions прошли; затем включены в общий прогон |
| MCP + plugin unit checks | 17 tests прошли без SDK и без skipped cases |
| Actual MCP protocol | Официальный SDK 2.3.0, stdio, protocol 2026-07-28; два tools, два успешных и один отрицательный вызов; private fixture не изменена |
| Plugin native install/discovery | Codex 0.166.0-alpha.5.1, отдельные homes; portable manifest и compatibility-only fallback; два skills обнаружены |
| CLI install/reinstall/uninstall | Отдельный пустой профиль; dry-run без targets, повторный план пустой, все harness-owned targets сняты при uninstall |
| CLI native verify 1.1 | Новый parser и prompt-input: marker политики найден, `native_input_verified` |
| Независимое review | Policy, source claims, installer migration, orchestration, adjacent gate, MCP, plugin и memory/token profiles проверены; подтверждённые findings исправлены и повторно рассмотрены |
| Portable release audit | JSON/TOML, metadata всех шести authored skills, относительные Markdown links, версии, portable/private exclusions и ограниченный secret-pattern scan |

Все native проверки выполнялись в isolated profiles и с разрешёнными fixtures.
Отсутствие SDK даёт failure, а не green skip. Собственные system skills/runtime
files, создаваемые Codex в пустом home, не принадлежат harness и сохраняются.
Plugin discovery не доказывает семантическое поведение skills с моделью.

## Исправления по независимому ревью

1. Пересечения writers проверяются по physical targets, вложенным cwd и общему
   checkout; filesystem probe сохраняет исходный регистр пути.
2. Dependency не выпускается по одному recorded status: проверяются обязательные
   results/logs/receipts/toolchain bindings, current source и frozen child linkage
   к ожидаемой node и текущим parent attempt/revision. Тот же read-only checker
   используется adjacent gate; есть cycle guard.
3. Brief/repair включают проверенные ссылки на общий контекст, reading sections
   и конкретные outputs. Это часть schema/renderer, а не устное дополнение root.
4. Обновление не оставляет исчезнувшие owned skills незаметно активными; миграция
   package location обновляет только известные overlay paths с rollback.
5. Source claims GSD и выбор Codex provider в memory profile уточнены по evidence.

## GitHub, установка и пределы приёмки

Финальные проверки выполняются на frozen commit. Затем отдельно наблюдаются
private visibility, совпадение remote/local/canonical heads, Actions и native
установка. Их фактические идентификаторы находятся в поставочной квитанции;
для репозитория доступны [Actions конкретных commits](https://github.com/SUKUNA-AI/agent-harness/actions).
Присутствие YAML и конфигурации не считается результатом исполнения.

CI задан для Windows/Ubuntu и Python 3.11/3.13: stdlib suite, release audit и
actual SDK smoke. Platform-specific Windows containment test на другой ОС не
подтверждается и должен оставаться явно skipped. Arch/macOS не квалифицированы.

Полный fresh-session native auto-spawn/repair/recovery benchmark этой версии
не выполнялся. В текущем чате наблюдалась реальная работа native owners в двух
изолированных worktrees, передача findings прежнему owner и интеграция repair;
это отдельный объём наблюдения, не гарантия поведения всех будущих запросов.
Local policy не настраивает hosted Work, не гарантирует hot reload старого чата
и не обещает работу после закрытия приложения.

Live MCP attachment и внешние accounts не подключены. Claude-Mem,
TokenEconomizer и прочие upstream прошли source screening, не runtime pilot.
Экономия токенов/ускорение не измерены. Summaries и metadata не заменяют primary
evidence. Secret scan ограничен известными patterns; это не доказательство
отсутствия любого возможного секрета или OS sandbox для недоверенного кода.
