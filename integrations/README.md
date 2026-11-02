# Интеграции: подключать по задаче

Справочник релиза **1.5.1**, актуальность **2026-11-02**. Сам harness использует
Python stdlib для управляющего слоя и файлы private state. Для базового пути не
нужны отдельная БД, daemon, импорт истории, внешний model API или аккаунты сервисов.
Модельная сессия самого Codex использует штатные model/provider и авторизацию
своей среды; установка harness их не заменяет.

Scanner/snapshot и RepoMap могут использовать bundled Rust helper. Python
snapshot fallback работает без него; syntactic RepoMap требует native backend.
Semantic code commands используют отдельно доступный language server, child
которого завершается после одной команды. Ни RepoMap, ни LSP не требуют MCP.

## Выбор минимально достаточной возможности

| Задача | Основной путь | Наблюдение перед использованием | Граница/fallback |
|---|---|---|---|
| Найти структуру репозитория | `context map --root ... --query ...` | Native backend, parser versions, hashes и partial scope | Адресный `rg`; map не доказывает semantic references |
| Symbols, definition, references, rename | `code ...` | `code doctor`, доступный local server и актуальные inputs | Явно установить нужный server; без него не обещать semantic result |
| Посмотреть записанный run status | Разрешённый CLI либо optional harness MCP | Явный store/run ID; `recorded_status`, freshness boundary | [Metadata MCP](../docs/MCP_RU.md) не делает gate/resume |
| Git, issues, PR, CI | Подключённый GitHub plugin | Нужный account/repository и read-only metadata | `git`/`gh` при достаточной capability и авторизации |
| Официальная документация | Web search и первоисточник | Конкретная открытая страница и дата | Optional docs MCP, если он нужен задаче |
| Browser/app interaction | Доступный purpose-built tool | Реальная страница, account, scope действия | Подходящий API/CLI; наличие браузера не означает login |
| Документы, PDF, таблицы, слайды | Artifact skills/tools | Файл, расчёты и просмотр оформления | Доступный локальный runtime с проверенными зависимостями |
| Drive/Notion/общие файлы | Нужный подключённый connector | Разрешённый объект и выбранный workspace | Локальные файлы; автоматическая синхронизация не подразумевается |
| Tasks/projects | Выбранный tracker или GitHub issues | Нужный workspace/проект | Private local run store для состояния самого harness |
| Почта, календарь, сообщения | Только нужный provider | Правильный account/recipient и разрешённая операция | Без задачи подключение не требуется |
| Удалённое выполнение | Штатный SSH/CLI/service connector | Версия, target и безопасная read-only команда | Локальная среда; production action требует основания |
| Данные и БД | Доступный SQL/data connector | Read-only schema и ограниченный запрос | Разрешённый экспорт; не копировать всю базу |

Наличие строки в таблице не означает установленную интеграцию. Перед каждой
задачей учитывайте реальные tools, account, permissions и доступность среды.
Подключённые сервисы одного компьютера нельзя переносить как подтверждённые
возможности другого. Свойства окружения записывают в private state, не в policy.

## Отconfigured к проверенной capability

1. Назовите конкретную потребность и выберите достаточный уже доступный tool.
2. Если интеграции нет, проверьте официальный endpoint/package, transport,
   зависимости и способ auth выбранного сервиса. Не устанавливайте пакет из
   случайного каталога ради совпадения названия.
3. Настройте только нужную интеграцию в выбранном профиле. Secrets хранятся
   штатно вне репозитория; не помещайте tokens в CLI args, TOML-примеры или логи.
4. Откройте новую сессию, подтвердите discovery tools и выполните узкий read-only
   запрос к правильному account/object. До этого статус остаётся configured/not_run.
5. Выполняйте запись, отправку, публикацию или production action только в scope
   текущей разрешённой задачи. Наличие connector не расширяет полномочия.

[capabilities.example.json](capabilities.example.json) — шаблон для наблюдений,
а не scanner или автоматический инсталлятор. Заполненный machine overlay,
private run data, credentials и персональные пути не возвращаются в repository.

## Готовые необязательные примеры

- [harness-mcp.example.toml](harness-mcp.example.toml) — local read-only metadata
  для одного private store. Требует отдельной venv с pinned optional extra;
  [инструкция MCP](../docs/MCP_RU.md) объясняет границы свежести и bounds.
- [openai-docs.example.toml](openai-docs.example.toml) — public documentation MCP
  endpoint из официального примера OpenAI. Добавляйте адресно только нужную table;
  сам файл ничего не подключает. Затем проверьте реальное получение нужной страницы.
- [Harness Assistant 1.3.1](../docs/PLUGIN_RU.md) — два portable skills без hooks/MCP.
  Его версия независима от harness 1.5.1; plugin update/installation и core
  harness installation выполняются отдельно.

Для GitHub сначала используйте достаточный подключённый plugin. При необходимости
CLI проверьте `git --version`, `gh --version`, `gh auth status`; последний может
показать account metadata, поэтому не публикуйте raw output без просмотра.
Штатный `gh auth login` выполняется отдельно пользователем/разрешённым flow.
Наличие `gh` не даёт доступа ко всем private repositories; read-only запрос
конкретной репы подтверждает только этот scope, не право создавать/push.

Основные понятия и discovery сверены с официальными
[Customization](https://learn.chatgpt.com/docs/customization/overview),
[Skills](https://learn.chatgpt.com/docs/build-skills) и
[Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents).
Клиентские capabilities меняются; при работе используйте актуальный первоисточник.
[SETUP_RU](../SETUP_RU.md) описывает базовую установку без внешних подключений.
