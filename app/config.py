"""Конфигурация приложения (pydantic-settings).

Группы настроек вынесены в отдельные модели, чтобы доменам было удобно
зависеть только от нужного среза конфигурации, а не от всего объекта.
"""

from __future__ import annotations

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


class Settings(BaseSettings):
    """Корневые настройки приложения."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_env: str = "local"
    app_debug: bool = False

    database_url: str
    redis_url: str = "redis://localhost:6379/0"

    security: SecuritySettings = Field(default_factory=SecuritySettings)
    esia: EsiaSettings = Field(default_factory=EsiaSettings)


@lru_cache
def get_settings() -> Settings:
    """Возвращает закэшированный singleton настроек."""
    return Settings()


SettingsDep = Annotated[Settings, Depends(get_settings)]
"""FastAPI-аннотация для инъекции настроек."""
