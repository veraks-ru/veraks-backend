"""Лёгкий smoke-тест обвязки ARQ-воркера (регистрация задач и расписания).

Бизнес-логика покрыта юнит-тестами координаторов; здесь — что воркер
импортируется и корректно регистрирует функции и cron-расписание.
"""

from __future__ import annotations

from app.worker import (
    WorkerSettings,
    recompute_ratings,
    score_event,
    season_roll,
)


def test_worker_registers_all_tasks() -> None:
    assert set(WorkerSettings.functions) == {
        score_event,
        recompute_ratings,
        season_roll,
    }


def test_worker_has_cron_schedule() -> None:
    # Ночной пересчёт + периодический roll сезонов.
    assert len(WorkerSettings.cron_jobs) == 2
