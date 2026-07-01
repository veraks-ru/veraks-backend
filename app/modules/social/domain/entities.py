"""Доменные сущности соцфич: комментарии, подписки, элемент ленты.

Чистый код без I/O. Комментарий — мягко удаляемый (``deleted_at``), подписка —
несимметричная связь follower→followee с запретом самоподписки. ``FeedItem`` —
read-модель ленты, собираемая шлюзом из разных доменов (comments/predictions).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.modules.social.domain.errors import (
    CommentEmptyError,
    CommentTooLongError,
    SelfFollowError,
)

_MAX_COMMENT_LEN = 2000


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class Comment:
    """Комментарий участника к событию (мягкое удаление автором/модератором)."""

    event_id: uuid.UUID
    author_id: uuid.UUID
    body: str
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=_utcnow)
    deleted_at: datetime | None = None

    @classmethod
    def create(
        cls,
        *,
        event_id: uuid.UUID,
        author_id: uuid.UUID,
        body: str,
        now: datetime | None = None,
    ) -> Comment:
        text = body.strip()
        if not text:
            raise CommentEmptyError("Комментарий не может быть пустым")
        if len(text) > _MAX_COMMENT_LEN:
            raise CommentTooLongError(
                f"Комментарий длиннее {_MAX_COMMENT_LEN} символов"
            )
        return cls(
            event_id=event_id,
            author_id=author_id,
            body=text,
            created_at=now or _utcnow(),
        )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def soft_delete(self, *, now: datetime | None = None) -> None:
        self.deleted_at = now or _utcnow()


@dataclass(slots=True)
class Follow:
    """Подписка ``follower`` на ``followee`` (нельзя подписаться на себя)."""

    follower_id: uuid.UUID
    followee_id: uuid.UUID
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if self.follower_id == self.followee_id:
            raise SelfFollowError("Нельзя подписаться на самого себя")


@dataclass(frozen=True, slots=True)
class FeedItem:
    """Элемент персональной ленты — активность отслеживаемого предсказателя.

    ``kind``: ``"comment"`` (прокомментировал событие) или ``"score"`` (его
    прогноз по событию засчитан). Поля-«хвосты» заполняются по типу.
    """

    kind: str
    actor_id: uuid.UUID
    actor_username: str
    actor_display_name: str
    event_id: uuid.UUID
    event_title: str
    occurred_at: datetime
    body: str | None = None
    brier: float | None = None
    outcome: bool | None = None
