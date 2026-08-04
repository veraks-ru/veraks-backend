# Бэкенд «Оракул» (в коде — «Веракс»)

Модульный монолит на FastAPI. Архитектура, конвенции слоёв, команды разработки
и тестирования — в [`CLAUDE.md`](./CLAUDE.md); модель данных и API — в
[`../ARCHITECTURE.md`](../ARCHITECTURE.md). Этот файл — эксплуатационные
заметки, которые не вписываются в гид для Claude Code.

## Роль БД приложения (`orakul_app`)

### Зачем

Append-only-журналы (`audit_log`, `resolutions`, `ledger_transactions`,
`ledger_entries`, `season_finalizations`, `season_finalization_entries`,
`user_consents`) защищены триггером `block_mutations()` — он физически
запрещает UPDATE/DELETE в схеме. Но триггер можно обойти, если у роли,
которой ходит приложение, есть привилегии `ALTER TABLE`/`DROP TRIGGER`, —
поэтому нужен **второй, независимый рубеж**: сама роль приложения не должна
иметь UPDATE/DELETE на этих таблицах. Именно это делает миграция
[`0011_revoke_append_only_grants`](alembic/versions/0011_revoke_append_only_grants.py)
(и её расширение
[`0029_extend_append_only_revoke`](alembic/versions/0029_extend_append_only_revoke.py)
для журналов, добавленных позже 0011) — но **только если роль `orakul_app`
уже существует**; если приложение ходит под владельцем схемы (как в
дефолтном локальном `.env.example`), REVOKE — no-op, и второй рубеж не
работает, журнал держится только на триггере.

### Как создать роль

Роль создаёт и обновляет **`scripts/create_app_role.py`** — идемпотентный
bootstrap-скрипт на `asyncpg` (без внешней зависимости от клиента `psql`,
которого может не быть в образе приложения). Выдаёт `orakul_app`:

- `CONNECT`/`USAGE` на БД/схему;
- `SELECT, INSERT, UPDATE, DELETE` на обычные таблицы;
- `SELECT, INSERT` **без** `UPDATE, DELETE` — на все append-only-журналы
  (актуальный список — константа `APPEND_ONLY_TABLES` в самом скрипте, она же
  зеркалируется в `scripts/create_app_role.sql`, в 0011/0029 и в
  `tests/e2e/test_db_role.py`);
- `USAGE, SELECT` на sequences;
- `ALTER DEFAULT PRIVILEGES` от имени владельца схемы — чтобы таблицы,
  создаваемые БУДУЩИМИ миграциями, сразу получали CRUD для `orakul_app` без
  ручных доработок (append-only-исключения всё равно защищены явным REVOKE в
  самой миграции по образцу 0011/0029 — это единственное, что нельзя
  предсказать заранее).

Запуск (после `alembic upgrade head`, **под владельцем схемы** — только у
него есть право GRANT/REVOKE/ALTER DEFAULT PRIVILEGES):

```bash
APP_DB_PASSWORD='сильный-пароль' python scripts/create_app_role.py
```

Подключается через `ALEMBIC_DATABASE_URL` (если задан отдельно от
прикладного `DATABASE_URL`, см. ниже), иначе — через `DATABASE_URL`. Имя роли
— `APP_DB_ROLE` (по умолчанию `orakul_app`).

Скрипт **идемпотентен**: повторный запуск ничего не ломает, пароль и гранты
переустанавливаются. Это НУЖНО делать после каждой миграции, добавляющей
таблицы, — новые обычные таблицы получат CRUD автоматически (см.
`ALTER DEFAULT PRIVILEGES` выше), но если появился новый append-only-журнал,
для него нужен REVOKE, который либо добавляется отдельной миграцией (как
0029), либо подхватывается перезапуском этого скрипта (его список
`APPEND_ONLY_TABLES` — источник истины, актуальный на момент запуска).

Ручной SQL-эквивалент для DBA (тот же алгоритм, через `psql -v`) —
[`scripts/create_app_role.sql`](scripts/create_app_role.sql); держать логику
синхронизированной при правках любого из двух файлов.

### Как переключить `DATABASE_URL`

Приложение (uvicorn, ARQ-воркер) должно подключаться **ролью `orakul_app`**,
а не владельцем. Миграции (`alembic upgrade head`) — наоборот, **только
владельцем** (нужны DDL-права). Раздельные URL:

```bash
DATABASE_URL=postgresql+asyncpg://orakul_app:пароль@host:5432/orakul
ALEMBIC_DATABASE_URL=postgresql+asyncpg://orakul:пароль-владельца@host:5432/orakul
```

`ALEMBIC_DATABASE_URL` читает `alembic/env.py` (приоритетно над
`DATABASE_URL`) — если не задан, миграции идут через `DATABASE_URL` (так
устроен локальный дев по умолчанию, см. `.env.example`: без разделения ролей
проще поднимать окружение, второй рубеж защиты там осознанно не работает).

**Порядок операций при деплое** (см. `infra/helm/veraks/templates/backend.yaml`
— чарт делает это тремя `initContainer`'ами):

1. Роль `orakul_app` создана/обновлена (`scripts/create_app_role.py` под
   владельцем) — ДО миграций, иначе REVOKE в 0011/0029 не сработает для ещё
   не созданных на тот момент таблиц.
2. `alembic upgrade head` (владелец).
3. `scripts/create_app_role.py` ещё раз (владелец) — подчищает гранты на
   случай, если список append-only расширился со времени прошлого деплоя.

### Проверка вживую

`tests/e2e/test_db_role.py` — e2e-тест против реального Postgres: бутстрапит
тестовую роль тем же `scripts/create_app_role.py` и проверяет, что под ней
SELECT/INSERT на append-only разрешены, а UPDATE/DELETE падают с
`permission denied` (до триггера дело не доходит — привилегий нет физически).
Требует выделенную БД с `e2e` в имени (см. `backend/CLAUDE.md`):

```bash
DATABASE_URL=postgresql+asyncpg://orakul:orakul@localhost:5432/orakul_e2e \
  pytest tests/e2e/test_db_role.py -v
```

Локальный docker-compose (`web/docker-compose.yml`) заводит роль `orakul_app`
«из коробки» через `docker-entrypoint-initdb.d` (см.
`web/dev/postgres-initdb/010-create-app-role.sh`) — она доступна для ручной
проверки, но `DATABASE_URL` приложения локально оставлен под владельцем
(удобство дева важнее второго рубежа защиты в dev-контуре).
