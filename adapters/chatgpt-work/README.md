# ChatGPT Work: граница локальной установки

Контракт релиза **1.5.0**, актуальность **2026-11-01**. Этот каталог документирует
границу применения, а не рабочий hosted installer.

[Общая политика](../../policies/global.md), [workflows](../../workflows/)
и [роли](../../roles/) описывают переносимые правила работы. Локальная команда
`python tools/harness.py install` пишет выбранный **Codex home** и пользовательский
skills home на компьютере. Она не подключает эти файлы к hosted ChatGPT Work,
обычному web-чату или другой удалённой сессии.

| Локальное наблюдение | Что можно заключить для Work |
|---|---|
| `structural_verified` | Локальная installation согласована; hosted discovery не проверен |
| `native_input_verified` | Новый локальный CLI получил marker/version; hosted input не наблюдался |
| Роли/skills доступны в Codex | У этого клиента есть discovery; наличие тех же capabilities в Work неизвестно |
| MCP/plugin настроен локально | Подключён локальный профиль; удалённый account/connector отдельно не подтверждён |

Для отдельной hosted-интеграции сначала определите поддерживаемый источник
постоянных инструкций, capabilities и permissions конкретного клиента. Затем
подключите разрешённые компактные правила штатным способом и выполните новую
приёмку: источник/версия инструкций, доступные tools/agents, фактическое выполнение
ограниченной задачи и соответствующее evidence. Не копируйте credentials,
machine pointer, private state или абсолютные пути локальной машины.

Локальный config не меняет hosted model/provider, reasoning, sandbox или лимиты.
Нельзя объявлять local CLI замещающим hosted backend без явно выбранного сценария.
Недоступность дополнительного backend не отменяет приёмку уже проверенного
локального пути, но его статус остаётся not_configured/not_run.

Различие guidance, skills, MCP и subagents описано в официальном
[Customization overview](https://learn.chatgpt.com/docs/customization/overview).
Конкретная поддержка зависит от текущего hosted продукта; эта поставка не
обещает hosted autoload или автоматическое восстановление чата.

[Локальная установка](../../SETUP_RU.md) ·
[Автоподключение Codex](../../docs/AUTOLOAD_RU.md) ·
[Интеграции](../../integrations/README.md).
