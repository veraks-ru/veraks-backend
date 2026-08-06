"""Юнит-тест нормализации наивных datetime в query-параметрах (``_as_utc``)."""

from __future__ import annotations

from datetime import datetime, timezone

from app.shared.audit.api.router import _as_utc


def test_naive_datetime_gets_utc_tzinfo() -> None:
    naive = datetime(2026, 1, 1, 12, 0, 0)
    result = _as_utc(naive)
    assert result is not None
    assert result.tzinfo == timezone.utc
    assert result.replace(tzinfo=None) == naive


def test_aware_datetime_is_left_untouched() -> None:
    from datetime import timedelta

    msk = timezone(timedelta(hours=3))
    aware = datetime(2026, 1, 1, 12, 0, 0, tzinfo=msk)
    assert _as_utc(aware) is aware


def test_none_passes_through() -> None:
    assert _as_utc(None) is None
