"""Конфигурация приложения (pydantic-settings).

Группы настроек вынесены в отдельные модели, чтобы доменам было удобно
зависеть только от нужного среза конфигурации, а не от всего объекта.
"""

from __future__ import annotations

from datetime import timedelta
from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from pydantic import Field
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

    monthly_price_kopecks: int = Field(default=49_000, ge=1)
    annual_price_kopecks: int = Field(default=490_000, ge=1)


class WebhookSettings(BaseSettings):
    """Секреты верификации подписи входящих вебхуков провайдеров.

    Пустой секрет означает «верификация выключена» (локальная разработка/тесты);
    в проде секреты обязательны — иначе подделанный вебхук мог бы провести
    платёж/выплату. Адаптер проверяет HMAC-SHA256 тела по этому секрету.
    """

    model_config = SettingsConfigDict(env_prefix="WEBHOOK_", extra="ignore")

    yookassa_payment_secret: str = ""
    yookassa_payout_secret: str = ""


class Settings(BaseSettings):
    """Корневые настройки приложения."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_env: str = "local"
    app_debug: bool = False

    database_url: str
    redis_url: str = "redis://localhost:6379/0"

    # Авто-финализация сезонов в таймерном ``season_roll``. Включена: боевой
    # ``ResolutionDisputeGuard`` (домен resolutions) блокирует финализацию сезона
    # с открытыми спорами, поэтому таймерное авто-закрытие безопасно (§6.4/§6.5).
    # Выключить можно через env, если нужно временно перевести на ручной режим.
    seasons_auto_finalize: bool = True

    security: SecuritySettings = Field(default_factory=SecuritySettings)
    esia: EsiaSettings = Field(default_factory=EsiaSettings)
    resolutions: ResolutionsSettings = Field(default_factory=ResolutionsSettings)
    billing: BillingSettings = Field(default_factory=BillingSettings)
    webhooks: WebhookSettings = Field(default_factory=WebhookSettings)


@lru_cache
def get_settings() -> Settings:
    """Возвращает закэшированный singleton настроек."""
    return Settings()


SettingsDep = Annotated[Settings, Depends(get_settings)]
"""FastAPI-аннотация для инъекции настроек."""
