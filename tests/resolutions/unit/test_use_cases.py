"""Юнит-тесты use-cases resolutions на in-memory фейках портов.

Покрывают полный поток: фиксация исхода → оспаривание → решение арбитра
(reject/overturn) → фоновое закрытие окна и постановка скоринга, а также
краевые случаи прав, окна и участия.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app.modules.events.domain.entities import EventStatus
from app.modules.resolutions.application.dto import Actor
from app.modules.resolutions.domain.entities import DisputeStatus, ResolutionStatus
from app.modules.resolutions.domain.errors import (
    DisputeAlreadyDecidedError,
    DisputeNotAllowedError,
    DisputeNotFoundError,
    DisputeWindowClosedError,
    EventNotResolvableError,
    InvalidResolutionDataError,
    ResolutionNotFoundError,
    ResolutionPermissionError,
    ResolutionTargetEventNotFoundError,
    SelfDisputeDecisionError,
)
from app.modules.identity.domain.entities import UserRole
from tests.resolutions.conftest import DISPUTE_WINDOW, FIXED_NOW


def _new_event() -> uuid.UUID:
    return uuid.uuid4()


# ── FixResolution ───────────────────────────────────────────────────────────


async def test_fix_resolution_resolves_event_and_opens_window(stand, editor) -> None:
    event_id = _new_event()
    stand.events.seed(event_id, status=EventStatus.CLOSED)

    resolution = await stand.fix.execute(
        event_id=event_id,
        actor=editor,
        outcome=True,
        source_reference="https://source.example/proof",
    )

    assert resolution.status is ResolutionStatus.FINAL
    assert resolution.outcome is True
    assert stand.events.status_of(event_id) is EventStatus.RESOLVED
    lifecycle = await stand.events.get_lifecycle(event_id)
    assert lifecycle.dispute_window_ends_at == FIXED_NOW + DISPUTE_WINDOW
    assert "resolution.finalized" in stand.audit.actions()


async def test_fix_requires_closed_event(stand, editor) -> None:
    event_id = _new_event()
    stand.events.seed(event_id, status=EventStatus.OPEN)
    with pytest.raises(EventNotResolvableError):
        await stand.fix.execute(
            event_id=event_id, actor=editor, outcome=True, source_reference="x"
        )


async def test_fix_unknown_event_raises(stand, editor) -> None:
    with pytest.raises(ResolutionTargetEventNotFoundError):
        await stand.fix.execute(
            event_id=_new_event(), actor=editor, outcome=True, source_reference="x"
        )


async def test_fix_forbidden_for_plain_user(stand, participant) -> None:
    event_id = _new_event()
    stand.events.seed(event_id, status=EventStatus.CLOSED)
    with pytest.raises(ResolutionPermissionError):
        await stand.fix.execute(
            event_id=event_id, actor=participant, outcome=True, source_reference="x"
        )


async def test_fix_rejects_empty_source(stand, editor) -> None:
    event_id = _new_event()
    stand.events.seed(event_id, status=EventStatus.CLOSED)
    with pytest.raises(InvalidResolutionDataError):
        await stand.fix.execute(
            event_id=event_id, actor=editor, outcome=True, source_reference="   "
        )


# ── GetResolution ───────────────────────────────────────────────────────────


async def test_get_resolution_missing_raises(stand) -> None:
    with pytest.raises(ResolutionNotFoundError):
        await stand.get.execute(event_id=_new_event())


async def test_get_resolution_returns_current(stand, editor) -> None:
    event_id = _new_event()
    stand.events.seed(event_id, status=EventStatus.CLOSED)
    await stand.fix.execute(
        event_id=event_id, actor=editor, outcome=False, source_reference="src"
    )
    current = await stand.get.execute(event_id=event_id)
    assert current.outcome is False


# ── RaiseDispute ────────────────────────────────────────────────────────────


async def _resolve(stand, editor, *, outcome: bool = True) -> uuid.UUID:
    """Заводит закрытое событие и фиксирует исход; возвращает event_id."""
    event_id = _new_event()
    stand.events.seed(event_id, status=EventStatus.CLOSED)
    await stand.fix.execute(
        event_id=event_id,
        actor=editor,
        outcome=outcome,
        source_reference="https://source.example",
    )
    return event_id


async def test_raise_dispute_moves_event_to_disputed(stand, editor, participant) -> None:
    event_id = await _resolve(stand, editor)
    stand.participation.allow(participant.user_id, event_id)

    dispute = await stand.raise_dispute.execute(
        event_id=event_id, actor=participant, reason="Источник противоречит исходу"
    )

    assert dispute.status is DisputeStatus.OPEN
    assert stand.events.status_of(event_id) is EventStatus.DISPUTED
    current = await stand.get.execute(event_id=event_id)
    assert dispute.resolution_id == current.id
    assert "dispute.raised" in stand.audit.actions()


async def test_raise_dispute_requires_participation(stand, editor, participant) -> None:
    event_id = await _resolve(stand, editor)
    with pytest.raises(DisputeNotAllowedError):
        await stand.raise_dispute.execute(
            event_id=event_id, actor=participant, reason="нет прогноза"
        )


async def test_raise_dispute_rejected_after_window(stand, editor, participant) -> None:
    event_id = _new_event()
    # Окно уже истекло относительно FIXED_NOW.
    stand.events.seed(
        event_id,
        status=EventStatus.RESOLVED,
        outcome=True,
        dispute_window_ends_at=FIXED_NOW - timedelta(hours=1),
    )
    stand.participation.allow(participant.user_id, event_id)
    with pytest.raises(DisputeWindowClosedError):
        await stand.raise_dispute.execute(
            event_id=event_id, actor=participant, reason="поздно"
        )


async def test_raise_dispute_rejected_when_not_resolved(stand, participant) -> None:
    event_id = _new_event()
    stand.events.seed(event_id, status=EventStatus.CLOSED)
    stand.participation.allow(participant.user_id, event_id)
    with pytest.raises(DisputeWindowClosedError):
        await stand.raise_dispute.execute(
            event_id=event_id, actor=participant, reason="событие не разрешено"
        )


# ── DecideDispute ───────────────────────────────────────────────────────────


async def _open_dispute(stand, editor, participant, *, outcome: bool = True):
    """Доводит событие до открытого спора; возвращает (event_id, dispute)."""
    event_id = await _resolve(stand, editor, outcome=outcome)
    stand.participation.allow(participant.user_id, event_id)
    dispute = await stand.raise_dispute.execute(
        event_id=event_id, actor=participant, reason="спорный исход"
    )
    return event_id, dispute


async def test_reject_returns_event_to_resolved(stand, editor, participant, arbiter):
    event_id, dispute = await _open_dispute(stand, editor, participant)

    decided = await stand.decide.execute(
        dispute_id=dispute.id, actor=arbiter, accept=False, decision_notes="без оснований"
    )

    assert decided.status is DisputeStatus.REJECTED
    assert decided.decided_by == arbiter.user_id
    assert stand.events.status_of(event_id) is EventStatus.RESOLVED
    # Исход не изменился — по-прежнему одно финальное решение.
    history = await stand.resolutions.list_for_event(event_id)
    assert len(history) == 1
    assert "dispute.rejected" in stand.audit.actions()


async def test_accept_overturns_outcome_with_supersedes(stand, editor, participant, arbiter):
    event_id, dispute = await _open_dispute(stand, editor, participant, outcome=True)
    original = await stand.get.execute(event_id=event_id)

    decided = await stand.decide.execute(
        dispute_id=dispute.id,
        actor=arbiter,
        accept=True,
        new_outcome=False,
        decision_notes="источник подтверждает обратное",
    )

    assert decided.status is DisputeStatus.ACCEPTED
    assert stand.events.status_of(event_id) is EventStatus.RESOLVED
    current = await stand.get.execute(event_id=event_id)
    assert current.outcome is False
    assert current.supersedes_id == original.id
    history = await stand.resolutions.list_for_event(event_id)
    assert len(history) == 2
    # Окно открыто заново относительно момента решения.
    lifecycle = await stand.events.get_lifecycle(event_id)
    assert lifecycle.dispute_window_ends_at == FIXED_NOW + DISPUTE_WINDOW
    assert "resolution.overturned" in stand.audit.actions()


async def test_accept_requires_new_outcome(stand, editor, participant, arbiter):
    _, dispute = await _open_dispute(stand, editor, participant)
    with pytest.raises(InvalidResolutionDataError):
        await stand.decide.execute(dispute_id=dispute.id, actor=arbiter, accept=True)


async def test_accept_rejects_overturn_to_same_outcome(stand, editor, participant, arbiter):
    # Overturn в тот же исход бессмыслен: лишняя superseding-строка + повторное
    # открытие окна. Должен быть отвергнут до записи ревизии.
    event_id, dispute = await _open_dispute(stand, editor, participant, outcome=True)
    with pytest.raises(InvalidResolutionDataError):
        await stand.decide.execute(
            dispute_id=dispute.id, actor=arbiter, accept=True, new_outcome=True
        )
    # История не выросла — ревизия не записана.
    history = await stand.resolutions.list_for_event(event_id)
    assert len(history) == 1


async def test_cannot_decide_own_dispute(stand, editor, participant):
    _, dispute = await _open_dispute(stand, editor, participant)
    # Тот же пользователь (по id) пытается решить свой спор, но с ролью арбитра.
    self_arbiter = Actor(user_id=participant.user_id, role=UserRole.ARBITER)
    with pytest.raises(SelfDisputeDecisionError):
        await stand.decide.execute(
            dispute_id=dispute.id, actor=self_arbiter, accept=False
        )


async def test_cannot_decide_twice(stand, editor, participant, arbiter):
    _, dispute = await _open_dispute(stand, editor, participant)
    await stand.decide.execute(dispute_id=dispute.id, actor=arbiter, accept=False)
    with pytest.raises(DisputeAlreadyDecidedError):
        await stand.decide.execute(dispute_id=dispute.id, actor=arbiter, accept=False)


async def test_decide_forbidden_for_editor(stand, editor, participant):
    _, dispute = await _open_dispute(stand, editor, participant)
    with pytest.raises(ResolutionPermissionError):
        await stand.decide.execute(dispute_id=dispute.id, actor=editor, accept=False)


async def test_decide_unknown_dispute(stand, arbiter):
    with pytest.raises(DisputeNotFoundError):
        await stand.decide.execute(dispute_id=uuid.uuid4(), actor=arbiter, accept=False)


# ── CloseDisputeWindows ─────────────────────────────────────────────────────


async def test_close_windows_enqueues_scoring(stand, editor) -> None:
    event_id = await _resolve(stand, editor)
    # Сдвигаем окно в прошлое относительно FIXED_NOW (имитация истечения).
    lifecycle = await stand.events.get_lifecycle(event_id)
    stand.events.seed(
        event_id,
        status=EventStatus.RESOLVED,
        outcome=lifecycle.outcome,
        dispute_window_ends_at=FIXED_NOW - timedelta(minutes=1),
    )

    dispatched = await stand.close_windows.execute()

    assert dispatched == 1
    assert stand.tasks.enqueued == [event_id]
    assert "event.scoring_enqueued" in stand.audit.actions()


async def test_close_windows_is_idempotent(stand, editor) -> None:
    event_id = await _resolve(stand, editor)
    lifecycle = await stand.events.get_lifecycle(event_id)
    stand.events.seed(
        event_id,
        status=EventStatus.RESOLVED,
        outcome=lifecycle.outcome,
        dispute_window_ends_at=FIXED_NOW - timedelta(minutes=1),
    )

    first = await stand.close_windows.execute()
    second = await stand.close_windows.execute()

    assert first == 1
    assert second == 0
    assert stand.tasks.enqueued == [event_id]


async def test_close_windows_skips_open_window(stand, editor) -> None:
    # Окно ещё открыто (в будущем) — скоринг не ставится.
    await _resolve(stand, editor)
    dispatched = await stand.close_windows.execute()
    assert dispatched == 0
    assert stand.tasks.enqueued == []


# ── M-RESRACE: блокировка строки события (FOR UPDATE) ─────────────────────────


async def test_fix_resolution_locks_event_row(stand, editor) -> None:
    # Фиксация исхода должна читать событие с блокировкой строки, чтобы две
    # конкурентные фиксации не создали двойную резолюцию.
    event_id = _new_event()
    stand.events.seed(event_id, status=EventStatus.CLOSED)
    await stand.fix.execute(
        event_id=event_id,
        actor=editor,
        outcome=True,
        source_reference="https://source.example",
    )
    assert event_id in stand.events.locked_reads


async def test_raise_dispute_locks_event_row(stand, editor, participant) -> None:
    # Подача спора должна читать событие с блокировкой строки, чтобы две
    # конкурентные подачи не открыли два спора по одному событию.
    event_id = await _resolve(stand, editor)
    stand.participation.allow(participant.user_id, event_id)
    await stand.raise_dispute.execute(
        event_id=event_id, actor=participant, reason="Источник противоречит исходу"
    )
    assert event_id in stand.events.locked_reads
