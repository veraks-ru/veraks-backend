"""Доменная сущность ``User`` и связанные перечисления.

Сущность намеренно не знает ни о SQLAlchemy, ни о pydantic — это обычный
dataclass. ORM-модель (adapters/orm.py) и API-схемы (api/schemas.py)
мапятся на неё, а не наоборот.
"""

from __future__ import annotations

import enum
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.modules.identity.domain.errors import InvalidUserStatusError


class UserRole(str, enum.Enum):
    """RBAC-роли (см. раздел безопасности: разделение обязанностей)."""

    USER = "user"
    EDITOR = "editor"
    ARBITER = "arbiter"
    ADMIN = "admin"


class UserStatus(str, enum.Enum):
    """Жизненный цикл аккаунта."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETED = "deleted"


def _utcnow() -> datetime:
    """Текущее время в UTC (источник времени — сервер)."""
    return datetime.now(timezone.utc)


def generate_username_seed() -> str:
    """Псевдонимный хэндл, НЕ производный от ФИО (приватность, PRD §4.1/§7.6).

    Реальное имя не попадает в публичный идентификатор (раньше хэндл строился из
    ФИО, а для кириллических имён вырождался в ``predictor``, деанонимизируя через
    display_name). Уникальность — на уровне БД (``UNIQUE(username)``); случайный
    хвост делает коллизии крайне маловероятными, но use-case всё равно
    переаллоцирует при UNIQUE-гонке.
    """
    return f"predictor-{secrets.token_hex(3)}"


@dataclass(slots=True)
class User:
    """Аккаунт, привязанный к верифицированному гражданину.

    ``snils_hash`` (HMAC от СНИЛС) — ключ инварианта «1 человек = 1 аккаунт».
    ``esia_oid_hash`` (HMAC от идентификатора ЕСИА) хранится по той же логике,
    что и ``snils_hash`` — сырой ``esia_oid`` в системе не персистится
    (152-ФЗ, минимизация ПДн); домен работает только с уже посчитанным хэшем,
    сам HMAC — забота адаптера (``HmacEsiaOidHasher``), не сущности.
    ``real_name_enc`` — зашифрованное ФИО; в публичный профиль не попадает.
    ``onboarded_at`` — момент прохождения онбординга (152-ФЗ: принятие оферты
    и согласия на ПДн + выбор псевдонима); ``None`` — онбординг ещё не пройден
    (в т.ч. у всех аккаунтов, созданных до появления этой фичи — они пройдут
    его при следующем входе).
    """

    esia_oid_hash: str
    snils_hash: str
    username: str
    display_name: str
    real_name_enc: bytes | None
    role: UserRole = UserRole.USER
    status: UserStatus = UserStatus.ACTIVE
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=_utcnow)
    onboarded_at: datetime | None = None

    @classmethod
    def register_from_esia(
        cls,
        *,
        esia_oid_hash: str,
        snils_hash: str,
        username: str,
        real_name_enc: bytes | None,
    ) -> User:
        """Фабрика нового аккаунта по данным ЕСИА (find-or-create: ветка create).

        Принимает уже посчитанные хэши (``esia_oid_hash``, ``snils_hash``) —
        HMAC от сырых значений считает use-case через порты, домен сырых
        ПДн не видит.

        ``display_name`` по умолчанию = псевдонимный ``username``: реальное ФИО
        (``real_name_enc``) публично не раскрывается (PRD §4.1/§7.6). Пользователь
        может задать отображаемое имя сам через ``PATCH /users/me``.
        """
        return cls(
            esia_oid_hash=esia_oid_hash,
            snils_hash=snils_hash,
            username=username,
            display_name=username,
            real_name_enc=real_name_enc,
        )

    def is_active(self) -> bool:
        """Может ли аккаунт пользоваться системой."""
        return self.status is UserStatus.ACTIVE

    def edit_profile(
        self, *, display_name: str | None = None, username: str | None = None
    ) -> bool:
        """Редактирует публичный профиль (то, чем владеет пользователь).

        ``display_name`` и ``username`` — пользовательские поля (PATCH
        /users/me и онбординг), поэтому при повторном входе ЕСИА их НЕ
        перезатирает (юридическое ФИО — отдельно в ``real_name_enc``).
        Уникальность ``username`` домен не проверяет — это забота репозитория
        (``UNIQUE(username)``, citext); use-case ловит нарушение и превращает
        его в доменную ошибку. Возвращает ``True``, если что-то изменилось.
        """
        changed = False
        if display_name is not None:
            new_display_name = display_name.strip()
            if new_display_name and new_display_name != self.display_name:
                self.display_name = new_display_name
                changed = True
        if username is not None:
            new_username = username.strip()
            if new_username and new_username != self.username:
                self.username = new_username
                changed = True
        return changed

    def complete_onboarding(self) -> None:
        """Фиксирует прохождение онбординга (согласия приняты, псевдоним задан)."""
        self.onboarded_at = _utcnow()

    def needs_onboarding(self, *, missing_consents: bool) -> bool:
        """Нужен ли онбординг: не пройден ИЛИ есть недостающие обязательные согласия.

        ``missing_consents`` считает вызывающий (обычно
        ``domain.policies.missing_consents``), т.к. это требует сверки со
        списком уже принятых документов, которого у самой сущности нет.
        """
        return self.onboarded_at is None or missing_consents

    def anonymize_for_deletion(self) -> bool:
        """Необратимо переводит аккаунт в ``DELETED`` и анонимизирует профиль (152-ФЗ).

        Стирает ФИО (``real_name_enc``) и освобождает публичный псевдоним
        (``username`` → детерминированное «надгробие» вида ``deleted-<8 hex
        от id>``, ``display_name`` → «Удалённый аккаунт»).

        ``snils_hash``/``esia_oid_hash`` НЕ трогаем — это ключ инварианта
        «1 человек = 1 аккаунт»: без них повторная регистрация того же
        гражданина после удаления обошла бы ограничение. Правомерность и срок
        хранения этих хэшей после удаления — вопрос к юристу (см.
        ``audit/04-human-playbooks.md`` §3 п.7); пока решение по умолчанию —
        хранить бессрочно, т.к. хэш необратим и сам по себе не раскрывает ПДн.

        Идемпотентна: для уже удалённого аккаунта — no-op, возвращает
        ``False`` (повторный вызов ничего не меняет и не проваливается).
        """
        if self.status is UserStatus.DELETED:
            return False
        self.status = UserStatus.DELETED
        self.real_name_enc = None
        self.display_name = "Удалённый аккаунт"
        self.username = f"deleted-{self.id.hex[:8]}"
        return True

    def suspend(self) -> None:
        """``active → suspended``: блокировка модерацией (B7).

        Кто вправе заблокировать (не себя, не другого админа) проверяет
        вызывающий — ``domain.policies.ensure_can_suspend``; сущность отвечает
        только за сам переход состояния и не пускает его из «неактивного»
        статуса (иначе неоднозначно, что означает повторная блокировка уже
        удалённого или уже заблокированного аккаунта).
        """
        if self.status is not UserStatus.ACTIVE:
            raise InvalidUserStatusError(
                "Заблокировать можно только активный аккаунт (текущий статус: "
                f"{self.status.value})"
            )
        self.status = UserStatus.SUSPENDED

    def reinstate(self) -> None:
        """``suspended → active``: снятие блокировки модерацией (B7)."""
        if self.status is not UserStatus.SUSPENDED:
            raise InvalidUserStatusError(
                "Разблокировать можно только заблокированный аккаунт (текущий "
                f"статус: {self.status.value})"
            )
        self.status = UserStatus.ACTIVE

    def apply_esia_refresh(
        self, *, esia_oid_hash: str, real_name_enc: bytes | None
    ) -> bool:
        """Обновляет данные при повторном входе (ЕСИА — источник истины по ФИО).

        Возвращает ``True``, если что-то изменилось (нужен ли UPDATE/аудит).
        Хэндл (username) пользователь меняет сам — здесь его не трогаем.
        """
        changed = False
        if self.esia_oid_hash != esia_oid_hash:
            # Хэш привязки не должен меняться при том же snils_hash, но фиксируем.
            self.esia_oid_hash = esia_oid_hash
            changed = True
        if real_name_enc is not None and real_name_enc != self.real_name_enc:
            self.real_name_enc = real_name_enc
            changed = True
        return changed
