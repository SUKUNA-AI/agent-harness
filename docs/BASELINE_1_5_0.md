# Исходная проверка 1.5.0

Дата: 2026-11-01. Проверена свежая `main`
`5f06a2fdebe48859189f285ad107a8c2c360579e`, версия 1.4.2.

| Проверка | Наблюдение |
| --- | --- |
| Python 3.13.13, Windows x64 | 393 tests, OK, 43 явно обозначенных платформенных пропуска; 339.499 s |
| `tools/release_check.py` | passed, 266 файлов, 6 skills |
| Rust 1.90.0, MSVC | 18 tests passed, release build passed |
| `tools/check_native.py` | passed: Python/native parity и RepoMap сценарии |
| Существующий installer | doctor, dry-run, apply, structural verify passed |
| `verify --native` | native_input_verified в отдельном процессе Codex |
| Codex CLI | 0.166.0-alpha.5.1 |
| Защита пользовательского TOML | Все ранее существовавшие значения сохранены; добавлен отсутствовавший compaction=500000; child cap=5 |

Исходных failures в выполненных проверках не обнаружено. Пропуски Windows
не подтверждают POSIX ACL/xattr/FIFO сценарии. Приватные установочные manifests,
исходные журналы и hashes сохранены вне репозитория. Этот baseline не является
приёмкой реализации 1.5.0 и не подтверждает поддержку иных платформ.
