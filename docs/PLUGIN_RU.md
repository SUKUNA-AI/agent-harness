# Необязательный Harness Assistant plugin

Документация harness **1.5.0**, plugin **1.3.1**, сверено **2026-11-01**.
Пакет находится в
[`plugins/harness-assistant/`](../plugins/harness-assistant/). Он распространяет
две процедуры как skills; основной local installer, глобальный policy bootstrap
и metadata MCP остаются отдельными механизмами.

## Состав и границы

| Часть | Что содержит |
|---|---|
| `plugin.json` | Portable identity, версия и OpenAI overlay в `extensions.com.openai` |
| `.codex-plugin/plugin.json` | Совместимый manifest Codex и путь `./skills/` |
| `skills/harness-coordinate/` | Результат/scope, пропорциональная координация и проверяемая приёмка |
| `skills/harness-run-status/` | Записанный run status через доступный read-only MCP либо CLI |

Обе формы manifest содержат пустые hooks. Plugin не содержит MCP config,
install scripts, модельный routing или изменения permissions. Он не включает
MCP автоматически и не заменяет [глобальную установку](../SETUP_RU.md).
Номер plugin **1.3.1** независим от общего релиза harness **1.5.0** и читается
из обоих manifests. Обновление документации/checkout само по себе не обновляет
уже установленную клиентом копию plugin; проверьте его фактическую версию и
примените штатный update/install flow клиента.

Формат пакета описан в
[официальной документации OpenAI](https://developers.openai.com/plugins/build/plugins).
Исходная схема проверена 31.10.2026; интерфейс установки зависит от доступной
версии клиента. Перед использованием проверьте `codex plugin --help`.

## Установка из постоянного локального checkout

Команды ниже меняют выбранный Codex profile. Для пробной установки используйте
изолированный `CODEX_HOME`; для рабочей установки выберите свой действующий
профиль осознанно. Это отдельный шаг от `python tools/harness.py install`.

POSIX shell, из package root:

```bash
HARNESS_PLUGIN_HOME="$(mktemp -d)"
CODEX_HOME="$HARNESS_PLUGIN_HOME" codex plugin --help
CODEX_HOME="$HARNESS_PLUGIN_HOME" codex plugin marketplace add "$PWD"
CODEX_HOME="$HARNESS_PLUGIN_HOME" codex plugin add harness-assistant@harness-local
CODEX_HOME="$HARNESS_PLUGIN_HOME" codex plugin list --json --marketplace harness-local
```

Windows PowerShell, из package root, в отдельном тестовом окне:

```powershell
$HarnessPluginHome = Join-Path ([System.IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString())
New-Item -ItemType Directory -Path $HarnessPluginHome | Out-Null
$env:CODEX_HOME = $HarnessPluginHome
codex plugin --help
codex plugin marketplace add "$((Get-Location).Path)"
codex plugin add harness-assistant@harness-local
codex plugin list --json --marketplace harness-local
```

PowerShell пример устанавливает `CODEX_HOME` только для текущего тестового окна
и его дочерних процессов. Закройте его после проверки; не запускайте из этого
окна рабочую сессию, ожидая другой профиль. При ошибке не продолжайте следующие
команды автоматически. Если CLI не поддерживает этот plugin flow, зафиксируйте
ограничение; копирование manifest-файлов не заменяет установку клиентом.

Marketplace описан в
[`.agents/plugins/marketplace.json`](../.agents/plugins/marketplace.json).
Его `source.path` разрешается от корня репозитория, поэтому сохраняйте package
в постоянном месте. Подключение private репозитория/аккаунта не предполагается
этими локальными командами.

## Проверка discovery и поведения

После установки откройте новую сессию именно с выбранным profile. Найдите оба
skills через catalog/picker и вызовите имя, которое показывает ваш клиент:

- `harness-coordinate`: на небольшой задаче вроде перевода должен выбирать
  прямое выполнение, без формального run и лишних агентов. Для полезных независимых
  частей — назначать owners в общем runtime budget и оставлять приёмку primary.
- `harness-run-status`: должен назвать источник recorded status и предел свежести.
  При metadata MCP сохраняются ограничения `recorded_status` и `not_checked`;
  отсутствие доступного CLI/MCP сообщается как ограничение, не исправляется
  выдуманным результатом.

| Уровень проверки | Достаточное наблюдение |
|---|---|
| Структура source package | Локальные tests, согласованные manifests, внутренние ссылки |
| Установка/discovery | CLI installation и запись обоих skills в фактическом catalog |
| Активация skill | Новый chat/runtime вызвал выбранную процедуру |
| Реальное spawning/приёмка | Наблюдаемые native refs, lifecycle и fresh evidence конкретной задачи |

Source tests из package root:

```text
python -B -m unittest discover -s tests -p test_plugin.py
```

Они проверяют standalone copy, marketplace root, manifest consistency,
внутренние ссылки и отсутствие hooks/MCP/machine-path substitutions. Они не
доказывают последние три runtime-уровня таблицы. Управляйте отключением/удалением
plugin штатными средствами вашей версии клиента; install state основного
harness и private run store от этого не удаляются.

[Конфигурация ролей](CONFIGURATION_RU.md) · [MCP](MCP_RU.md) ·
[Граница hosted Work](../adapters/chatgpt-work/README.md).
