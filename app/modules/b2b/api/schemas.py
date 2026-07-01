"""Pydantic-схемы B2B signal API."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.modules.b2b.application.dto import (
    ConsensusSignal,
    EventSignal,
    LeaderboardSignalRow,
)
from app.modules.b2b.application.use_cases import ApiKeyUsage, IssuedApiKey
from app.modules.b2b.domain.entities import ApiKey


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    daily_quota: int | None = Field(default=None, ge=1)


class ApiKeyResponse(BaseModel):
    id: uuid.UUID
    name: str
    key_prefix: str
    daily_quota: int
    is_active: bool
    created_at: datetime
    revoked_at: datetime | None
    used_today: int | None = None

    @classmethod
    def from_domain(
        cls, k: ApiKey, *, used_today: int | None = None
    ) -> "ApiKeyResponse":
        return cls(
            id=k.id,
            name=k.name,
            key_prefix=k.key_prefix,
            daily_quota=k.daily_quota,
            is_active=k.is_active,
            created_at=k.created_at,
            revoked_at=k.revoked_at,
            used_today=used_today,
        )

    @classmethod
    def from_usage(cls, u: ApiKeyUsage) -> "ApiKeyResponse":
        return cls.from_domain(u.key, used_today=u.used_today)


class IssuedApiKeyResponse(BaseModel):
    """Ответ выдачи: ключ + ПОЛНЫЙ секрет (показывается один раз)."""

    key: ApiKeyResponse
    secret: str

    @classmethod
    def from_issued(cls, issued: IssuedApiKey) -> "IssuedApiKeyResponse":
        return cls(
            key=ApiKeyResponse.from_domain(issued.key), secret=issued.plaintext
        )


# ── Сигналы ──────────────────────────────────────────────────────────────────


class ConsensusSignalResponse(BaseModel):
    event_id: uuid.UUID
    total_count: int
    mean_probability: float | None
    distribution: dict[str, int]

    @classmethod
    def from_signal(cls, s: ConsensusSignal) -> "ConsensusSignalResponse":
        return cls(
            event_id=s.event_id,
            total_count=s.total_count,
            mean_probability=s.mean_probability,
            distribution=s.distribution,
        )


class LeaderboardSignalRowResponse(BaseModel):
    rank: int
    user_id: uuid.UUID
    username: str
    skill_score: str
    mean_brier: str
    n_resolved: int

    @classmethod
    def from_row(cls, r: LeaderboardSignalRow) -> "LeaderboardSignalRowResponse":
        return cls(
            rank=r.rank,
            user_id=r.user_id,
            username=r.username,
            skill_score=str(r.skill_score),
            mean_brier=str(r.mean_brier),
            n_resolved=r.n_resolved,
        )


class EventSignalResponse(BaseModel):
    id: uuid.UUID
    title: str
    category_id: uuid.UUID
    season_id: uuid.UUID | None
    status: str
    opens_at: datetime
    closes_at: datetime
    resolves_at: datetime
    outcome: bool | None

    @classmethod
    def from_signal(cls, s: EventSignal) -> "EventSignalResponse":
        return cls(
            id=s.id,
            title=s.title,
            category_id=s.category_id,
            season_id=s.season_id,
            status=s.status,
            opens_at=s.opens_at,
            closes_at=s.closes_at,
            resolves_at=s.resolves_at,
            outcome=s.outcome,
        )
