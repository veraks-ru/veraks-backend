"""ORM-модели лиг и дивизионов."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import CITEXT, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.modules.leagues.domain.entities import (
    Division,
    DivisionMembership,
    League,
    LeagueMembership,
)


class LeagueORM(Base):
    """Приватная лига."""

    __tablename__ = "leagues"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    invite_code: Mapped[str] = mapped_column(
        CITEXT, unique=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    def to_domain(self) -> League:
        return League(
            id=self.id,
            name=self.name,
            owner_id=self.owner_id,
            invite_code=self.invite_code,
            created_at=self.created_at,
        )

    @classmethod
    def from_domain(cls, x: League) -> "LeagueORM":
        return cls(
            id=x.id,
            name=x.name,
            owner_id=x.owner_id,
            invite_code=x.invite_code,
            created_at=x.created_at,
        )


class LeagueMembershipORM(Base):
    """Участие пользователя в приватной лиге."""

    __tablename__ = "league_memberships"
    __table_args__ = (
        UniqueConstraint("league_id", "user_id", name="uq_league_member"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    league_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leagues.id"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    joined_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    def to_domain(self) -> LeagueMembership:
        return LeagueMembership(
            id=self.id,
            league_id=self.league_id,
            user_id=self.user_id,
            joined_at=self.joined_at,
        )

    @classmethod
    def from_domain(cls, x: LeagueMembership) -> "LeagueMembershipORM":
        return cls(
            id=x.id,
            league_id=x.league_id,
            user_id=x.user_id,
            joined_at=x.joined_at,
        )


class DivisionORM(Base):
    """Уровень системной лестницы дивизионов."""

    __tablename__ = "divisions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    level: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)

    def to_domain(self) -> Division:
        return Division(id=self.id, level=self.level, title=self.title)

    @classmethod
    def from_domain(cls, x: Division) -> "DivisionORM":
        return cls(id=x.id, level=x.level, title=x.title)


class DivisionMembershipORM(Base):
    """Дивизион пользователя в конкретном сезоне."""

    __tablename__ = "division_memberships"
    __table_args__ = (
        UniqueConstraint("user_id", "season_id", name="uq_division_member"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    season_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("seasons.id"), nullable=False, index=True
    )
    division_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("divisions.id"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    def to_domain(self) -> DivisionMembership:
        return DivisionMembership(
            id=self.id,
            user_id=self.user_id,
            season_id=self.season_id,
            division_id=self.division_id,
            created_at=self.created_at,
        )

    @classmethod
    def from_domain(cls, x: DivisionMembership) -> "DivisionMembershipORM":
        return cls(
            id=x.id,
            user_id=x.user_id,
            season_id=x.season_id,
            division_id=x.division_id,
            created_at=x.created_at,
        )
