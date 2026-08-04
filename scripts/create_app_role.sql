-- Bootstrap непривилегированной роли БД приложения (T9).
--
-- Второй контур защиты append-only-журналов поверх триггеров
-- block_mutations() (миграции 0008 audit_log, 0009 resolutions,
-- 0010 ledger_transactions/ledger_entries, 0021 season_finalizations/
-- season_finalization_entries, 0025 user_consents): у роли приложения нет
-- UPDATE/DELETE на эти таблицы физически, на уровне привилегий. Миграция
-- 0011 (и 0029 — расширение на таблицы, добавленные после неё) делает REVOKE
-- при условии, что роль уже существует — поэтому создавать роль нужно ДО
-- очередного `alembic upgrade head` либо перевыполнять этот скрипт после.
--
-- РЕКОМЕНДУЕТСЯ запускать через scripts/create_app_role.py (тот же алгоритм
-- на asyncpg, без зависимости от клиента psql — его может не быть в образе
-- приложения). Этот .sql — эквивалент для ручного запуска DBA через psql
-- (и его же вызывает init-скрипт web/dev/postgres-initdb/010-create-app-role.sh
-- для локального docker-compose):
--
--   psql "$ALEMBIC_DATABASE_URL" \
--     -v app_role="orakul_app" -v app_password="секрет" \
--     -f scripts/create_app_role.sql
--
-- Подключаться нужно ВЛАДЕЛЬЦЕМ схемы (тем же, кем накатываются миграции) —
-- только у него есть право GRANT/REVOKE и ALTER DEFAULT PRIVILEGES.
-- Идемпотентно: повторный запуск ничего не ломает (перевыдаёт пароль и
-- гранты — так же нужно делать после каждой миграции, добавляющей таблицы).
--
-- Реализовано через генерацию DDL в SELECT + \gexec, а НЕ через DO $$ $$:
-- psql подставляет :'app_role'/:'app_password' только в «обычном» тексте
-- запроса — внутри dollar-quoted тела PL/pgSQL (``$$ ... $$``) подстановки
-- не происходит (проверено эмпирически на psql 15), поэтому вся логика
-- вынесена в динамически собираемые команды.

\set ON_ERROR_STOP on

-- 1. Роль (LOGIN, пароль из переменной app_password) — создать либо обновить.
SELECT format('ALTER ROLE %I LOGIN PASSWORD %L', :'app_role', :'app_password')
WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_role')
UNION ALL
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'app_role', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_role');
\gexec

-- 2. CONNECT на текущую БД + USAGE на схему public.
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'app_role')
UNION ALL
SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'app_role');
\gexec

-- 3. Таблицы: полный CRUD, КРОМЕ append-only (SELECT/INSERT без UPDATE/DELETE).
--    Список append-only держать в синхронизации с триггерами block_mutations()
--    (0008/0009/0010/0021/0025) — см. header выше.
WITH append_only(tablename) AS (
    VALUES ('audit_log'), ('resolutions'), ('ledger_transactions'),
           ('ledger_entries'), ('season_finalizations'),
           ('season_finalization_entries'), ('user_consents')
)
SELECT format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO %I', t.tablename, :'app_role')
FROM pg_tables t
WHERE t.schemaname = 'public' AND t.tablename NOT IN (SELECT tablename FROM append_only)
UNION ALL
SELECT format('GRANT SELECT, INSERT ON %I TO %I', t.tablename, :'app_role')
FROM pg_tables t
WHERE t.schemaname = 'public' AND t.tablename IN (SELECT tablename FROM append_only)
UNION ALL
SELECT format('REVOKE UPDATE, DELETE ON %I FROM %I', t.tablename, :'app_role')
FROM pg_tables t
WHERE t.schemaname = 'public' AND t.tablename IN (SELECT tablename FROM append_only);
\gexec

-- 4. Sequences (bigserial PK и т.п.) — USAGE нужен для nextval() при INSERT.
SELECT format('GRANT USAGE, SELECT ON SEQUENCE %I TO %I', sequencename, :'app_role')
FROM pg_sequences
WHERE schemaname = 'public';
\gexec

-- 5. ALTER DEFAULT PRIVILEGES — таблицы/sequences, которые владелец создаст
--    БУДУЩИМИ миграциями, сразу получают CRUD для роли приложения. Append-only
--    среди них всё равно защищены REVOKE в самих миграциях (по образцу 0011) —
--    здесь заранее не угадать, какая будущая таблица окажется append-only,
--    поэтому обязательно перезапускать этот скрипт после миграций,
--    добавляющих новые append-only-журналы.
SELECT format(
    'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
    'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I',
    current_user, :'app_role'
)
UNION ALL
SELECT format(
    'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
    'GRANT USAGE, SELECT ON SEQUENCES TO %I',
    current_user, :'app_role'
);
\gexec
