"""Доменные сущности events: ``Event``, ``Category`` и автомат статусов.

Сущности намеренно не знают ни о SQLAlchemy, ни о pydantic — это обычные
dataclass'ы. ORM-модели (``adapters/orm.py``) и API-схемы (``api/schemas.py``)
мапятся на них через явные ``to_domain``/``from_domain``, а не наследованием.

Ядро домена — конечный автомат жизненного цикла события: разрешённые
переходы заданы декларативно в ``_ALLOWED_TRANSITIONS`` и проверяются при
каждой смене статуса.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.modules.events.domain.errors import (
    EventEditNotAllowedError,
    InvalidEventTransitionError,
)
from app.modules.events.domain.value_objects import (
    EventWindow,
    require_text,
    validate_slug,
)


class EventStatus(str, enum.Enum):
    """Жизненный цикл события (бинарный исход в MVP).

    Переходы:
        draft → open → closed → resolving → resolved → disputed
    с возможностью ``cancelled`` из любого незавершённого статуса. Подробная
    карта — в :data:`_ALLOWED_TRANSITIONS`.
    """

    DRAFT = "draft"
    OPEN = "open"
    CLOSED = "closed"
    RESOLVING = "resolving"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"
    DISPUTED = "disputed"


# Декларативная карта допустимых переходов конечного автомата.
# Источник перехода → множество возможных целевых статусов.
_ALLOWED_TRANSITIONS: dict[EventStatus, frozenset[EventStatus]] = {
    EventStatus.DRAFT: frozenset({EventStatus.OPEN, EventStatus.CANCELLED}),
    EventStatus.OPEN: frozenset({EventStatus.CLOSED, EventStatus.CANCELLED}),
    EventStatus.CLOSED: frozenset({EventStatus.RESOLVING, EventStatus.CANCELLED}),
    EventStatus.RESOLVING: frozenset({EventStatus.RESOLVED}),
    EventStatus.RESOLVED: frozenset({EventStatus.DISPUTED}),
    # Оспаривание может вернуть событие к разрешению (overturn → ре-скоринг).
    EventStatus.DISPUTED: frozenset({EventStatus.RESOLVED}),
    EventStatus.CANCELLED: frozenset(),
}

# Статусы, в которых редакция вправе править содержание события.
_EDITABLE_STATUSES: frozenset[EventStatus] = frozenset(
    {EventStatus.DRAFT, EventStatus.OPEN}
)


def _utcnow() -> datetime:
    """Текущее время в UTC (источник времени — сервер)."""
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class Category:
    """Категория событий; дерево строится через ``parent_id`` (FK на себя)."""

    slug: str
    title: str
    description: str
    parent_id: uuid.UUID | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)

    @classmethod
    def create(
        cls,
        *,
        slug: str,
        title: str,
        description: str = "",
        parent_id: uuid.UUID | None = None,
    ) -> Category:
        """Фабрика категории с валидацией slug и обязательного заголовка."""
        return cls(
            slug=validate_slug(slug),
            title=require_text(title, field="title", max_length=200),
            description=description.strip(),
            parent_id=parent_id,
        )


@dataclass(slots=True)
class Event:
    """Прогнозируемое событие, создаваемое редакцией.

    Денормализованный ``outcome`` и ``resolved_at`` проставляются доменом
    разрешений (resolutions) при подведении исхода; здесь они только хранятся
    и читаются. Источник истины по разрешению — заранее заданные
    ``resolution_source`` и ``resolution_criteria``.
    """

    title: str
    description: str
    category_id: uuid.UUID
    created_by: uuid.UUID
    window: EventWindow
    resolution_source: str
    resolution_criteria: str
    season_id: uuid.UUID | None = None
    status: EventStatus = EventStatus.DRAFT
    outcome: bool | None = None
    resolved_at: datetime | None = None
    dispute_window_ends_at: datetime | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    # ── Фабрика ────────────────────────────────────────────────────────────

    @classmethod
    def create_draft(
        cls,
        *,
        title: str,
        description: str,
        category_id: uuid.UUID,
        created_by: uuid.UUID,
        window: EventWindow,
        resolution_source: str,
        resolution_criteria: str,
        season_id: uuid.UUID | None = None,
        now: datetime | None = None,
    ) -> Event:
        """Создаёт черновик события с провалидированными полями.

        Событие всегда рождается в статусе ``draft``; публикация — отдельный
        явный переход (:meth:`publish`). Корректность временного окна
        гарантирована типом :class:`EventWindow`.
        """
        moment = now or _utcnow()
        return cls(
            title=require_text(title, field="title", max_length=300),
            description=require_text(description, field="description"),
            category_id=category_id,
            created_by=created_by,
            window=window,
            resolution_source=require_text(
                resolution_source, field="resolution_source"
            ),
            resolution_criteria=require_text(
                resolution_criteria, field="resolution_criteria"
            ),
            season_id=season_id,
            status=EventStatus.DRAFT,
            created_at=moment,
            updated_at=moment,
        )

    # ── Редактирование ──────────────────────────────────────────────────────

    def apply_edits(
        self,
        *,
        title: str | None = None,
        description: str | None = None,
        category_id: uuid.UUID | None = None,
        season_id: uuid.UUID | None = None,
        window: EventWindow | None = None,
        resolution_source: str | None = None,
        resolution_criteria: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Применяет частичные правки (``None`` — поле не меняется).

        Правила по статусам:
          * редактировать можно только в ``draft`` и ``open``;
          * после публикации (``open``) категория и окно фиксируются — менять
            нельзя, чтобы не подрывать честность уже принятых прогнозов;
          * в ``draft`` правится всё.

        Возвращает ``True``, если что-то реально изменилось (нужен ли UPDATE
        и запись в audit_log).

        TODO(events-audit): фиксировать diff (before/after) в audit_log.
        """
        if self.status not in _EDITABLE_STATUSES:
            raise EventEditNotAllowedError(
                f"Редактирование запрещено в статусе «{self.status.value}»"
            )
        locked = self.status is EventStatus.OPEN
        if locked:
            if category_id is not None and category_id != self.category_id:
                raise EventEditNotAllowedError(
                    "Категорию нельзя менять после публикации события"
                )
            if window is not None and window != self.window:
                raise EventEditNotAllowedError(
                    "Временное окно нельзя менять после публикации события"
                )

        changed = False
        if title is not None:
            new_title = require_text(title, field="title", max_length=300)
            changed |= new_title != self.title
            self.title = new_title
        if description is not None:
            new_desc = require_text(description, field="description")
            changed |= new_desc != self.description
            self.description = new_desc
        if category_id is not None and category_id != self.category_id:
            self.category_id = category_id
            changed = True
        if season_id is not None and season_id != self.season_id:
            self.season_id = season_id
            changed = True
        if window is not None and window != self.window:
            self.window = window
            changed = True
        if resolution_source is not None:
            new_src = require_text(resolution_source, field="resolution_source")
            changed |= new_src != self.resolution_source
            self.resolution_source = new_src
        if resolution_criteria is not None:
            new_crit = require_text(
                resolution_criteria, field="resolution_criteria"
            )
            changed |= new_crit != self.resolution_criteria
            self.resolution_criteria = new_crit

        if changed:
            self.updated_at = now or _utcnow()
        return changed

    # ── Переходы жизненного цикла ───────────────────────────────────────────

    def publish(self, *, now: datetime | None = None) -> None:
        """``draft → open``: открывает приём прогнозов.

        Публиковать можно лишь событие, окно приёма которого ещё не истекло.
        """
        moment = now or _utcnow()
        if self.window.closes_at <= moment:
            raise InvalidEventTransitionError(
                "Нельзя опубликовать событие с истёкшим окном приёма (closes_at)"
            )
        self._transition_to(EventStatus.OPEN, now=moment)

    def close(self, *, now: datetime | None = None) -> None:
        """``open → closed``: прекращает приём прогнозов (блокировка).

        Вызывается редакцией вручную или системным воркером по ``closes_at``.

        TODO(events-infra): периодический воркер закрывает события по
        наступлению ``closes_at`` (см. поток жизненного цикла).
        """
        self._transition_to(EventStatus.CLOSED, now=now)

    def cancel(self, *, now: datetime | None = None) -> None:
        """``{draft, open, closed} → cancelled``: отмена события редакцией."""
        self._transition_to(EventStatus.CANCELLED, now=now)

    def begin_resolution(self, *, now: datetime | None = None) -> None:
        """``closed → resolving``: старт подведения исхода.

        Инициируется доменом resolutions (через ``EventResolutionGateway``),
        а не events-API.
        """
        self._transition_to(EventStatus.RESOLVING, now=now)

    def record_outcome(
        self,
        *,
        outcome: bool,
        dispute_window_ends_at: datetime,
        now: datetime | None = None,
    ) -> None:
        """``resolving|disputed → resolved``: фиксирует исход и окно оспаривания.

        Денормализованные ``outcome``/``resolved_at`` и ``dispute_window_ends_at``
        проставляются доменом resolutions при подведении исхода и при его
        пересмотре по принятому спору (overturn из ``disputed`` с новым исходом
        и заново открытым окном).
        """
        moment = now or _utcnow()
        self._transition_to(EventStatus.RESOLVED, now=moment)
        self.outcome = outcome
        self.resolved_at = moment
        self.dispute_window_ends_at = dispute_window_ends_at

    def open_dispute(self, *, now: datetime | None = None) -> None:
        """``resolved → disputed``: на событие подано оспаривание в пределах окна."""
        self._transition_to(EventStatus.DISPUTED, now=now)

    def dismiss_dispute(self, *, now: datetime | None = None) -> None:
        """``disputed → resolved``: спор отклонён, исход и окно сохраняются."""
        self._transition_to(EventStatus.RESOLVED, now=now)

    def _transition_to(self, target: EventStatus, *, now: datetime | None) -> None:
        """Проверяет переход по карте автомата и применяет его."""
        if target not in _ALLOWED_TRANSITIONS[self.status]:
            raise InvalidEventTransitionError(
                f"Недопустимый переход «{self.status.value}» → «{target.value}»"
            )
        self.status = target
        self.updated_at = now or _utcnow()

    def can_accept_predictions(self, *, now: datetime | None = None) -> bool:
        """Принимает ли событие прогнозы прямо сейчас (для домена predictions).

        TODO(predictions): домен прогнозов опирается на этот предикат и на
        серверный ``closes_at`` при валидации постановки прогноза.
        """
        moment = now or _utcnow()
        return self.status is EventStatus.OPEN and self.window.is_accepting_at(moment)
