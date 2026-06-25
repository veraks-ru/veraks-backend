"""Value-objects домена identity.

Содержит ``Snils`` (с валидацией контрольной суммы) и иммутабельные
снимки данных, приходящих из ЕСИА. Это чистый код — его можно покрывать
юнит-тестами в полной изоляции от FastAPI и БД.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.modules.identity.domain.errors import InvalidSnilsError

_SNILS_DIGITS_RE = re.compile(r"\d")


def _normalize_snils(raw: str) -> str:
    """Убирает разделители (пробелы/дефисы), оставляя только цифры."""
    return "".join(_SNILS_DIGITS_RE.findall(raw))


def _snils_checksum(first_nine: str) -> int:
    """Контрольное число СНИЛС по первым 9 цифрам (алгоритм ПФР)."""
    total = sum(int(d) * (9 - i) for i, d in enumerate(first_nine))
    if total < 100:
        return total
    if total in (100, 101):
        return 0
    remainder = total % 101
    return 0 if remainder in (100, 101) else remainder


@dataclass(frozen=True, slots=True)
class Snils:
    """Нормализованный СНИЛС с проверенной контрольной суммой.

    Хранит только цифры (11 знаков). Сырой СНИЛС в системе нигде не
    персистится открытым текстом — этот VO живёт лишь в памяти на время
    обработки логина, после чего превращается в HMAC-хеш.
    """

    digits: str

    def __post_init__(self) -> None:
        if len(self.digits) != 11 or not self.digits.isdigit():
            raise InvalidSnilsError("СНИЛС должен содержать ровно 11 цифр")
        # СНИЛС с номерами <= 001-001-998 контрольным числом не проверяются.
        number = int(self.digits[:9])
        if number > 1001998:
            expected = _snils_checksum(self.digits[:9])
            if expected != int(self.digits[9:]):
                raise InvalidSnilsError("Неверная контрольная сумма СНИЛС")

    @classmethod
    def parse(cls, raw: str) -> Snils:
        """Создаёт ``Snils`` из произвольного представления (с разделителями)."""
        if not raw or not raw.strip():
            raise InvalidSnilsError("Пустой СНИЛС")
        return cls(_normalize_snils(raw))

    def formatted(self) -> str:
        """Человекочитаемый формат ``XXX-XXX-XXX YY``."""
        d = self.digits
        return f"{d[0:3]}-{d[3:6]}-{d[6:9]} {d[9:11]}"


@dataclass(frozen=True, slots=True)
class EsiaTokens:
    """Маркеры, полученные на этапе обмена authorization code."""

    access_token: str
    id_token: str | None = None
    expires_in: int | None = None


@dataclass(frozen=True, slots=True)
class EsiaIdentity:
    """Иммутабельный снимок атрибутов гражданина из ЕСИА.

    ``trusted`` — признак «подтверждённой» учётной записи (КЭП/банк/МФЦ);
    упрощённую и стандартную учётки мы отклоняем (см. политику логина).
    """

    oid: str
    snils: Snils
    first_name: str
    last_name: str
    middle_name: str | None
    trusted: bool

    def full_name(self) -> str:
        """Собирает ФИО в строку (для шифрованного хранения)."""
        parts = [self.last_name, self.first_name, self.middle_name or ""]
        return " ".join(p for p in parts if p).strip()
