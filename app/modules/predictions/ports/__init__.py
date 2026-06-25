"""Порты домена predictions — абстрактные интерфейсы (Protocol).

Прикладной слой зависит от этих контрактов, а не от конкретных адаптеров
(SQLAlchemy, шлюз к events, аудит, часы). Реализации связываются в
composition root (``api/dependencies.py``).
"""
