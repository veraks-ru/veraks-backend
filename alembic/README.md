# Миграции (Alembic)

URL БД берётся из `app.config` (env-переменная `DATABASE_URL`).

```bash
alembic upgrade head          # применить все миграции
alembic downgrade -1          # откатить одну
alembic revision -m "msg"     # новая ревизия (autogenerate: --autogenerate)
```

`alembic/env.py` импортирует ORM-модели доменов, чтобы `--autogenerate`
видел полную метадату. Новый домен → добавить импорт его `orm`-модуля в `env.py`.
