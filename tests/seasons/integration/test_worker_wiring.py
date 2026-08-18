"""Лёгкий smoke-тест обвязки ARQ-воркера (регистрация задач и расписания).

Бизнес-логика покрыта юнит-тестами координаторов; здесь — что воркер
импортируется и корректно регистрирует функции и cron-расписание.
"""

from __future__ import annotations

from app.worker import (
    WorkerSettings,
    charge_due_subscriptions,
    close_dispute_windows,
    close_expired_events,
    dispatch_approved_payouts,
    poll_jump_payouts,
    recompute_ratings,
    reconcile,
    score_event,
    season_roll,
    verify_audit_chain,
)


def test_worker_registers_all_tasks() -> None:
    assert set(WorkerSettings.functions) == {
        score_event,
        recompute_ratings,
        season_roll,
        close_dispute_windows,
        close_expired_events,
        reconcile,
        dispatch_approved_payouts,
        poll_jump_payouts,
        verify_audit_chain,
        charge_due_subscriptions,
    }


def test_worker_has_cron_schedule() -> None:
    # Ночной пересчёт + roll сезонов + закрытие окон оспаривания + авто-закрытие
    # приёма по дедлайну + почасовая сверка журнала + авто-отправка выплат Jump
    # + опрос статусов выплат Jump + автосписание за продление подписок
    # + ночная верификация цепочки аудита.
    assert len(WorkerSettings.cron_jobs) == 9
