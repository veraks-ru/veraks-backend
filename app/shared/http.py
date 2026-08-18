"""Исходящие HTTP-клиенты с доверием к российским корневым сертификатам.

Эквайринг ТБанка перешёл на сертификаты НУЦ Минцифры («Russian Trusted Root
CA»). Их нет ни в системном хранилище образа, ни в ``certifi``, которым
пользуется httpx, поэтому обращение к ``securepay.tinkoff.ru`` падало с
``CERTIFICATE_VERIFY_FAILED: self-signed certificate in certificate chain`` —
оплата не начиналась вовсе.

Корни лежат в репозитории (``backend/certs``), а не скачиваются на сборке:
сборка идёт на зарубежных раннерах, откуда российский источник может быть
недоступен, и падение сборки из-за чужого сайта нам не нужно. Сверить их с
опубликованными на gosuslugi.ru/crt можно по отпечатку.

Контекст создаётся поверх ``certifi``, а не вместо него: остальные интеграции
(Jump, goctopus, ЕСИА) ходят к обычным сертификатам и должны продолжать
работать.
"""

from __future__ import annotations

import ssl
from functools import lru_cache
from pathlib import Path

import certifi
import httpx

#: Корни НУЦ Минцифры: корневой и промежуточный (developer.tbank.ru →
#: «Переход на Russian Trusted CA»).
CERTS_DIR = Path(__file__).resolve().parents[2] / "certs"
RUSSIAN_ROOTS = (
    CERTS_DIR / "russian_trusted_root_ca.pem",
    CERTS_DIR / "russian_trusted_sub_ca.pem",
)


@lru_cache(maxsize=1)
def trusted_ssl_context() -> ssl.SSLContext:
    """TLS-контекст: обычные корни ``certifi`` плюс корни НУЦ Минцифры.

    Кэшируется: разбор хранилища заметно дороже создания клиента, а состав
    доверенных корней в пределах процесса не меняется.
    """
    context = ssl.create_default_context(cafile=certifi.where())
    for pem in RUSSIAN_ROOTS:
        if pem.is_file():
            context.load_verify_locations(cafile=str(pem))
    return context


def http_client(*, timeout: float) -> httpx.AsyncClient:
    """Клиент для исходящих запросов платформы.

    Единая точка: проверка сертификатов не должна зависеть от того, кто и где
    создал клиент.
    """
    return httpx.AsyncClient(timeout=timeout, verify=trusted_ssl_context())
