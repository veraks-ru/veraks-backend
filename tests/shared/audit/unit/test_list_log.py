"""Юнит-тесты постраничного чтения аудит-журнала (``ListAuditLog``)."""

from __future__ import annotations

from app.shared.audit.application.list_log import ListAuditLog
from tests.shared.audit.fakes import FakeAuditLogReader, build_valid_chain


async def test_first_page_respects_limit_and_has_more() -> None:
    entries = build_valid_chain(5)
    uc = ListAuditLog(reader=FakeAuditLogReader(entries))

    page = await uc.execute(limit=2)

    assert [e.id for e in page.items] == [5, 4]  # новые сначала
    assert page.has_more is True


async def test_last_page_has_no_more() -> None:
    entries = build_valid_chain(3)
    uc = ListAuditLog(reader=FakeAuditLogReader(entries))

    page = await uc.execute(before_id=2, limit=50)

    assert [e.id for e in page.items] == [1]
    assert page.has_more is False


async def test_filters_by_action() -> None:
    entries = build_valid_chain(3)
    uc = ListAuditLog(reader=FakeAuditLogReader(entries))

    page = await uc.execute(action="test.action.2")

    assert [e.id for e in page.items] == [2]
