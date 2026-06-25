"""Декларативная база SQLAlchemy.

Все ORM-модели доменов наследуются от единого ``Base``, чтобы Alembic
видел общую метадату при автогенерации/проверке миграций.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Общая декларативная база для всех модулей монолита."""
