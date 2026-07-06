"""Конфигурация приложения (pydantic-settings).

Группы настроек вынесены в отдельные модели, чтобы доменам было удобно
зависеть только от нужного среза конфигурации, а не от всего объекта.
"""

from __future__ import annotations

from datetime import timedelta
from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SecuritySettings(BaseSettings):
    """Криптография ПДн и параметры JWT-сессий."""

    model_config = SettingsConfigDict(env_prefix="SECURITY_", extra="ignore")

    snils_hmac_key: str = Field(min_length=32)
    field_encryption_key: str = Field(min_length=32)

    jwt_secret: str = Field(min_length=32)
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 30 * 24 * 3600

    cookie_secure: bool = True
    cookie_domain: str | None = None


class EsiaSettings(BaseSettings):
    """Параметры подключения к ЕСИА (через сертифицированный шлюз)."""

    model_config = SettingsConfigDict(env_prefix="ESIA_", extra="ignore")

    client_id: str
    redirect_uri: str
    authorization_endpoint: str
    token_endpoint: str
    userinfo_endpoint: str
    scopes: str = "openid snils fullname"
    # Требовать «подтверждённую» учётную запись ЕСИА (отклонять упрощённую/стандартную).
    require_confirmed: bool = True

    @property
    def scope_list(self) -> list[str]:
        """Список scope'ов из строки, разделённой пробелами."""
        return self.scopes.split()


class ResolutionsSettings(BaseSettings):
    """Параметры разрешения событий и окна оспаривания."""

    model_config = SettingsConfigDict(env_prefix="RESOLUTIONS_", extra="ignore")

    # Длительность окна оспаривания после фиксации (и пересмотра) исхода.
    dispute_window_hours: int = Field(default=72, ge=0)

    @property
    def dispute_window(self) -> timedelta:
        """Окно оспаривания как ``timedelta``."""
        return timedelta(hours=self.dispute_window_hours)


class BillingSettings(BaseSettings):
    """Параметры платежей и двух касс.

    Цены тарифов — в копейках (никаких float). Composition root billing читает
    эти настройки и собирает из них карту «тариф → цена».
    """

    model_config = SettingsConfigDict(env_prefix="BILLING_", extra="ignore")

    daily_price_kopecks: int = Field(default=9_900, ge=1)
    weekly_price_kopecks: int = Field(default=49_900, ge=1)
    monthly_price_kopecks: int = Field(default=99_000, ge=1)
    annual_price_kopecks: int = Field(default=499_000, ge=1)


class RealtimeSettings(BaseSettings):
    """Пуш in-app уведомлений в реальном времени через goctopus (WS-релей).

    Пустой ``url`` = пуш выключен (уведомления только в БД). Бэкенд шлёт POST на
    goctopus с ключом = user_id; фронт получает по WebSocket.
    """

    model_config = SettingsConfigDict(env_prefix="GOCTOPUS_", extra="ignore")

    url: str = ""
    user: str = ""
    password: str = ""


class WebhookSettings(BaseSettings):
    """Секреты верификации подписи входящих вебхуков провайдеров.

    Пустой секрет означает «верификация выключена» (локальная разработка/тесты);
    в проде секреты обязательны — иначе подделанный вебхук мог бы провести
    платёж/выплату. Адаптер проверяет HMAC-SHA256 тела по этому секрету.
    """

    model_config = SettingsConfigDict(env_prefix="WEBHOOK_", extra="ignore")

    yookassa_payment_secret: str = ""
    yookassa_payout_secret: str = ""


class B2bSettings(BaseSettings):
    """Параметры B2B signal API: квоты и цена выдачи ключа.

    ``default_daily_quota`` — суточный лимит запросов на ключ (если у ключа нет
    своего). ``key_price_kopecks`` — разовая выручка при выдаче ключа (проводка
    ``b2b_invoice`` в операционную кассу).
    """

    model_config = SettingsConfigDict(env_prefix="B2B_", extra="ignore")

    default_daily_quota: int = Field(default=1_000, ge=1)
    key_price_kopecks: int = Field(default=490_000, ge=1)


class Settings(BaseSettings):
    """Корневые настройки приложения."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_env: str = "local"
    app_debug: bool = False

    database_url: str
    redis_url: str = "redis://localhost:6379/0"

    # Rate limiting (ARCHITECTURE.md §6): лимит запросов с одного IP в минуту.
    # Включается вне ``local`` (в тестах/деве не мешает). 0 — выключено.
    rate_limit_per_minute: int = Field(default=300, ge=0)

    # Авто-финализация сезонов в таймерном ``season_roll``. Включена: боевой
    # ``ResolutionDisputeGuard`` (домен resolutions) блокирует финализацию сезона
    # с открытыми спорами, поэтому таймерное авто-закрытие безопасно (§6.4/§6.5).
    # Выключить можно через env, если нужно временно перевести на ручной режим.
    seasons_auto_finalize: bool = True

    security: SecuritySettings = Field(default_factory=SecuritySettings)
    esia: EsiaSettings = Field(default_factory=EsiaSettings)
    realtime: RealtimeSettings = Field(default_factory=RealtimeSettings)
    resolutions: ResolutionsSettings = Field(default_factory=ResolutionsSettings)
    billing: BillingSettings = Field(default_factory=BillingSettings)
    webhooks: WebhookSettings = Field(default_factory=WebhookSettings)
    b2b: B2bSettings = Field(default_factory=B2bSettings)

    @model_validator(mode="after")
    def _require_webhook_secrets_in_prod(self) -> "Settings":
        """Вне ``local`` секреты вебхуков обязательны (fail-closed).

        Пустой секрет отключает проверку подписи (``verify_signature`` вернёт
        ``True`` на что угодно) — в проде это открывает подделку платежей и
        выплат. Поэтому при ``app_env != local`` приложение не поднимется без них.
        """
        if self.app_env != "local":
            missing = [
                name
                for name, value in (
                    ("WEBHOOK_YOOKASSA_PAYMENT_SECRET", self.webhooks.yookassa_payment_secret),
                    ("WEBHOOK_YOOKASSA_PAYOUT_SECRET", self.webhooks.yookassa_payout_secret),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    f"В окружении '{self.app_env}' обязательны секреты вебхуков: "
                    f"{', '.join(missing)} (пустой секрет отключает проверку подписи "
                    "и открывает подделку платежей/выплат)."
                )
        return self


@lru_cache
def get_settings() -> Settings:
    """Возвращает закэшированный singleton настроек."""
    return Settings()


SettingsDep = Annotated[Settings, Depends(get_settings)]
"""FastAPI-аннотация для инъекции настроек."""
