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
from datetime import UTC, datetime

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
    return datetime.now(UTC)


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
    """Аккаунт участника — с государственной идентификацией или без неё.

    Способов регистрации два, и они дают разный набор заполненных полей:

    * **ЕСИА** — ``snils_hash`` (HMAC от СНИЛС) как ключ инварианта
      «1 человек = 1 аккаунт», ``esia_oid_hash`` (HMAC от идентификатора
      ЕСИА) по той же логике; сырые значения в системе не персистятся
      (152-ФЗ, минимизация ПДн), домен работает только с готовыми хэшами, сам
      HMAC — забота адаптера (``HmacEsiaOidHasher``), не сущности.
      ``identity_verified=True``.
    * **email + одноразовая ссылка** — заполнен только ``email``, хэшей нет,
      ``identity_verified=False``.

    ``identity_verified`` — «личность подтверждена государственной
    идентификацией». Разделение статусов обязательно: PRD §7 связывает
    выплату приза с идентификацией личности, а участие открыто всем. Пока
    ЕСИА выключена, ``True`` не выставляется никому новому — это осознанное
    состояние, а не недоделка.

    ``real_name_enc`` — зашифрованное ФИО; в публичный профиль не попадает.
    ``onboarded_at`` — момент прохождения онбординга (152-ФЗ: принятие оферты
    и согласия на ПДн + выбор псевдонима); ``None`` — онбординг ещё не пройден
    (в т.ч. у всех аккаунтов, созданных до появления этой фичи — они пройдут
    его при следующем входе).

    Уникальность ``email`` (частичный UNIQUE по не-NULL) домен не проверяет —
    это забота репозитория, как и с ``username``.
    """

    username: str
    display_name: str
    real_name_enc: bytes | None
    esia_oid_hash: str | None = None
    snils_hash: str | None = None
    email: str | None = None
    identity_verified: bool = False
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

        ``identity_verified=True``: ЕСИА и есть государственная идентификация
        личности, на которую опирается PRD §7 при выплате приза.
        """
        return cls(
            esia_oid_hash=esia_oid_hash,
            snils_hash=snils_hash,
            username=username,
            display_name=username,
            real_name_enc=real_name_enc,
            identity_verified=True,
        )

    @classmethod
    def register_with_email(cls, *, email: str, username: str) -> User:
        """Фабрика аккаунта, заведённого по email-ссылке (find-or-create: ветка create).

        Ни СНИЛС, ни идентификатора ЕСИА у такого аккаунта нет — ссылка на
        почту не доказывает личность, поэтому ``identity_verified=False``:
        участвовать можно, а на выплату приза потребуется идентификация
        (PRD §7). ``email`` принимается уже нормализованным (см.
        ``domain.value_objects.normalize_email``) — сущность не занимается
        разбором пользовательского ввода.

        ``onboarded_at`` остаётся ``None``: согласия (оферта + ПДн)
        обязательны и собираются онбордингом, а не фактом получения письма.
        """
        return cls(
            username=username,
            display_name=username,
            real_name_enc=None,
            email=email,
            identity_verified=False,
        )

    def change_email(self, email: str) -> bool:
        """Задаёт адрес (смена по обращению в поддержку — только админом).

        Возвращает ``True``, если адрес действительно изменился. Сам себе
        пользователь email не меняет: подтверждение владения новым ящиком
        мы не реализуем, а без него смена адреса — это угон аккаунта.
        ``identity_verified`` смена адреса не трогает: подтверждение личности
        даёт ЕСИА, а не почта.
        """
        if email == self.email:
            return False
        self.email = email
        return True

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

        ``snils_hash``/``esia_oid_hash``/``email`` НЕ трогаем — это ключи
        инварианта «1 человек = 1 аккаунт»: без них повторная регистрация того
        же человека после удаления обошла бы ограничение (вход по email искал
        бы аккаунт по адресу и, не найдя, завёл бы новый вместо отказа
        ``AccountDeletedError``). Правомерность и срок хранения этих значений
        после удаления — вопрос к юристу (см. ``audit/04-human-playbooks.md``
        §3 п.7); для хэшей решение по умолчанию — хранить бессрочно, т.к. хэш
        необратим и сам по себе не раскрывает ПДн, а вот ``email`` хранится
        как есть и это отдельный вопрос к юристу (см. отчёт по задаче).

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
