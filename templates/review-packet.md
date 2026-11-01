# Пакет независимого ревью

- Запрошенный результат, scope и критерии:
- Base revision и actual candidate / contract digest:
- Diff и изменённые файлы:
- Нужные соседние интерфейсы и source references:
- Свежие проверки: команда, exit code, candidate/attempt и пути к raw evidence:
- Для file apply/compensation: actual status, plan digest, recovery receipt/journal и сохранённые backups:
- Известные ограничения / not_run / omitted files:
- Доступные команды и read-only границы:

Первый reviewer получает задачу, diff и raw evidence без авторского вердикта.
Проверить контрпример, конечный flow и регрессии; сверить входную revision/hashes
после проверки. Не принимать skipped/not_run за успешный required критерий. Замечание содержит путь, условие,
наблюдение, последствие, приоритет и способ проверки исправления. Отсутствие
подтверждённых замечаний допустимо. Root возвращает одну repair-подборку прежнему
owner и проверяет затронутые сценарии на итоговом candidate.
