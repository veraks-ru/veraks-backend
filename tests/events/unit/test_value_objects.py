"""Юнит-тесты value-objects events: окно события и slug категории."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.modules.events.domain.errors import (
    InvalidEventDataError,
    InvalidEventWindowError,
)
from app.modules.events.domain.value_objects import (
    EventWindow,
    require_text,
    validate_slug,
)

_NOW = datetime(2026, 6, 25, tzinfo=timezone.utc)


def test_valid_window_accepts_ordered_aware_dates() -> None:
    window = EventWindow(
        opens_at=_NOW,
        closes_at=_NOW + timedelta(days=1),
        resolves_at=_NOW + timedelta(days=2),
    )
    assert window.is_accepting_at(_NOW + timedelta(hours=1)) is True
    assert window.is_accepting_at(_NOW + timedelta(days=1)) is False


def test_window_rejects_naive_datetime() -> None:
    with pytest.raises(InvalidEventWindowError):
        EventWindow(
            opens_at=datetime(2026, 6, 25),  # naive
            closes_at=_NOW + timedelta(days=1),
            resolves_at=_NOW + timedelta(days=2),
        )


def test_window_rejects_close_before_open() -> None:
    with pytest.raises(InvalidEventWindowError):
        EventWindow(
            opens_at=_NOW + timedelta(days=2),
            closes_at=_NOW + timedelta(days=1),
            resolves_at=_NOW + timedelta(days=3),
        )


def test_window_rejects_resolve_before_close() -> None:
    with pytest.raises(InvalidEventWindowError):
        EventWindow(
            opens_at=_NOW,
            closes_at=_NOW + timedelta(days=2),
            resolves_at=_NOW + timedelta(days=1),
        )


@pytest.mark.parametrize("raw", ["Politics", "  sport-news  ", "tech2026"])
def test_validate_slug_normalizes(raw: str) -> None:
    assert validate_slug(raw) == raw.strip().lower()


@pytest.mark.parametrize("raw", ["", "  ", "bad slug", "под_черк", "-leading"])
def test_validate_slug_rejects_invalid(raw: str) -> None:
    with pytest.raises(InvalidEventDataError):
        validate_slug(raw)


def test_require_text_trims_and_validates() -> None:
    assert require_text("  hi  ", field="x") == "hi"
    with pytest.raises(InvalidEventDataError):
        require_text("   ", field="x")
