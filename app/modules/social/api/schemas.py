"""Pydantic-схемы соцфич."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.modules.social.application.use_cases import (
    CommentView,
    SocialStats,
)
from app.modules.social.domain.entities import FeedItem
from app.modules.social.ports.repositories import UserRef


class CommentCreateRequest(BaseModel):
    body: str = Field(min_length=1, max_length=2000)


class AuthorRef(BaseModel):
    user_id: uuid.UUID
    username: str
    display_name: str


class CommentResponse(BaseModel):
    id: uuid.UUID
    event_id: uuid.UUID
    body: str
    created_at: datetime
    author: AuthorRef | None

    @classmethod
    def from_view(cls, view: CommentView) -> "CommentResponse":
        c = view.comment
        author = (
            AuthorRef(
                user_id=view.author.id,
                username=view.author.username,
                display_name=view.author.display_name,
            )
            if view.author is not None
            else None
        )
        return cls(
            id=c.id,
            event_id=c.event_id,
            body=c.body,
            created_at=c.created_at,
            author=author,
        )


class UserRefResponse(BaseModel):
    user_id: uuid.UUID
    username: str
    display_name: str

    @classmethod
    def from_ref(cls, ref: UserRef) -> "UserRefResponse":
        return cls(
            user_id=ref.id, username=ref.username, display_name=ref.display_name
        )


class SocialStatsResponse(BaseModel):
    user_id: uuid.UUID
    followers: int
    following: int
    is_following: bool

    @classmethod
    def from_stats(cls, s: SocialStats) -> "SocialStatsResponse":
        return cls(
            user_id=s.user_id,
            followers=s.followers,
            following=s.following,
            is_following=s.is_following,
        )


class FeedItemResponse(BaseModel):
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

    @classmethod
    def from_item(cls, it: FeedItem) -> "FeedItemResponse":
        return cls(
            kind=it.kind,
            actor_id=it.actor_id,
            actor_username=it.actor_username,
            actor_display_name=it.actor_display_name,
            event_id=it.event_id,
            event_title=it.event_title,
            occurred_at=it.occurred_at,
            body=it.body,
            brier=it.brier,
            outcome=it.outcome,
        )
