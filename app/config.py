"""Конфигурация приложения (pydantic-settings).

Группы настроек вынесены в отдельные модели, чтобы доменам было удобно
зависеть только от нужного среза конфигурации, а не от всего объекта.
"""

from __future__ import annotations

from datetime import timedelta
from functools import lru_cache
from typing import Annotated, Literal

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


class ConsentsSettings(BaseSettings):
    """Реестр обязательных юридических документов и их текущих версий (152-ФЗ).

    Версия — дата редакции документа, опубликованного на ``/legal``. Юрист
    меняет версию через env (``CONSENTS_OFFER_VERSION``/``CONSENTS_PDN_VERSION``)
    — у всех пользователей, принявших более старую версию, тут же появляется
    недостающее согласие (``needs_onboarding=true`` в ``GET /auth/me``).
    """

    model_config = SettingsConfigDict(env_prefix="CONSENTS_", extra="ignore")

    offer_version: str = "2026-07-05"
    pdn_version: str = "2026-07-05"

    @property
    def required_documents(self) -> dict[str, str]:
        """Пары «слаг документа → обязательная версия»."""
        return {"offer": self.offer_version, "pdn": self.pdn_version}


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

    ``checkout_provider``/``payout_provider`` — явный выбор платёжного шлюза
    (composition root — ``billing/api/dependencies.py``). Раньше при
    незаданных ``tbank.enabled``/``jump.enabled`` сборка молча уходила в
    мёртвые ЮKassa-адаптеры (``NotImplementedError`` только в рантайме на
    первом платеже); теперь провайдер — явное значение, а не побочный эффект
    флага, и вне ``local`` проверяется валидатором ``Settings`` при старте.
    ``manual`` — осмысленное состояние «выплаты отправляются вручную, без
    провайдера», а не мёртвый фолбэк.
    """

    model_config = SettingsConfigDict(env_prefix="BILLING_", extra="ignore")

    daily_price_kopecks: int = Field(default=9_900, ge=1)
    weekly_price_kopecks: int = Field(default=49_900, ge=1)
    monthly_price_kopecks: int = Field(default=99_000, ge=1)
    annual_price_kopecks: int = Field(default=499_000, ge=1)

    checkout_provider: Literal["local", "tbank"] = "local"
    payout_provider: Literal["manual", "jump"] = "manual"


class TBankSettings(BaseSettings):
    """Эквайринг ТБанк (приём платежей за подписку, hosted-форма банка, nonPCI).

    Секреты (``terminal_key``/``password``) — только из env/K8s-секрета, не в git.
    ``enabled=False`` — интеграция выключена (поведение как прежде). Подпись Token и
    приём вебхука используют ``password`` (см. domain/tbank_signing.py).
    """

    model_config = SettingsConfigDict(env_prefix="TBANK_", extra="ignore")

    enabled: bool = False
    terminal_key: str = ""
    password: str = ""
    api_base_url: str = "https://securepay.tinkoff.ru/v2"
    # СНО для чека 54-ФЗ. ИП на УСН «доходы» → usn_income.
    taxation: str = "usn_income"
    # E-mail для чека 54-ФЗ (ЕСИА не даёт почту плательщика). Пусто → Receipt в
    # Init не отправляется. Заполнить, когда к терминалу подключена онлайн-касса.
    receipt_email: str = ""


class JumpSettings(BaseSettings):
    """Выплаты победителям через Jump.Finance (касса PRIZE, СБП по телефону).

    ``api_key`` — Client-Key из ЛК Jump (Настройки → Интеграции → OpenAPI);
    показывается один раз, только из env/секрета. Песочницы у Jump нет:
    безопасное тестирование — режим «Требующие подтверждения» в ЛК (выплата
    создаётся, деньги не двигаются до ручного подтверждения). Вебхуков нет —
    статусы опрашивает воркер. ``enabled=False`` — интеграция выключена.
    """

    model_config = SettingsConfigDict(env_prefix="JUMP_", extra="ignore")

    enabled: bool = False
    api_key: str = ""
    api_base_url: str = "https://api.jump.finance/services/openapi"
    # id юрлица и счёта в Jump (GET /banks_accounts); фиксируются в env.
    agent_id: int | None = None
    bank_account_id: int | None = None
    # Правовая форма исполнителя: 1 — физлицо (НДФЛ удерживает платформа).
    legal_form_id: int = 1


class RealtimeSettings(BaseSettings):
    """Пуш in-app уведомлений в реальном времени через goctopus (WS-релей).

    Пустой ``url`` = пуш выключен (уведомления только в БД). Бэкенд шлёт POST на
    goctopus с ключом = user_id; фронт получает по WebSocket.
    """

    model_config = SettingsConfigDict(env_prefix="GOCTOPUS_", extra="ignore")

    url: str = ""
    user: str = ""
    password: str = ""


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
    # Владелец схемы — используется ТОЛЬКО для Alembic-миграций (T9: приложение
    # в проде подключается непривилегированной ролью ``orakul_app`` через
    # ``database_url``, а DDL и ``scripts/create_app_role.py`` требуют владельца).
    # Если не задан — миграции тоже идут через ``database_url`` (локальный дев
    # без разделения ролей, см. .env.example).
    alembic_database_url: str | None = None
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
    consents: ConsentsSettings = Field(default_factory=ConsentsSettings)
    realtime: RealtimeSettings = Field(default_factory=RealtimeSettings)
    resolutions: ResolutionsSettings = Field(default_factory=ResolutionsSettings)
    billing: BillingSettings = Field(default_factory=BillingSettings)
    b2b: B2bSettings = Field(default_factory=B2bSettings)
    tbank: TBankSettings = Field(default_factory=TBankSettings)
    jump: JumpSettings = Field(default_factory=JumpSettings)

    # Публичные базовые URL для платёжных редиректов и вебхуков ТБанк.
    public_web_base: str = "https://veraks.ru"
    public_api_base: str = "https://api.veraks.ru"

    @model_validator(mode="after")
    def _require_billing_providers_in_prod(self) -> "Settings":
        """Вне ``local`` платёжные провайдеры обязаны быть явно и полно настроены.

        Прецедент — этот же паттерн раньше стоял на секретах вебхуков ЮKassa
        (ныне не используются: интеграции не будет). Проблема, которую решает
        именно эта проверка: при незаданных ``tbank.enabled``/``jump.enabled``
        composition root (``billing/api/dependencies.py``) раньше молча собирал
        приложение с мёртвыми ЮKassa-адаптерами (``create_checkout``/
        ``send_payout`` кидали ``NotImplementedError`` только в рантайме, на
        первом платеже). Теперь провайдер выбирается явно
        (``BILLING_CHECKOUT_PROVIDER``/``BILLING_PAYOUT_PROVIDER``), и вне
        ``local`` он обязан указывать на реальную, полностью настроенную
        интеграцию — иначе приложение не поднимется.
        """
        if self.app_env == "local":
            return self

        if self.billing.checkout_provider != "tbank":
            raise ValueError(
                "Вне окружения 'local' BILLING_CHECKOUT_PROVIDER должен быть "
                f"'tbank' (сейчас {self.billing.checkout_provider!r}): локальная "
                "заглушка оплаты недопустима вне local."
            )
        missing_checkout = [
            name
            for name, value in (
                ("TBANK_ENABLED", "true" if self.tbank.enabled else ""),
                ("TBANK_TERMINAL_KEY", self.tbank.terminal_key),
                ("TBANK_PASSWORD", self.tbank.password),
            )
            if not value
        ]
        if missing_checkout:
            raise ValueError(
                f"В окружении '{self.app_env}' BILLING_CHECKOUT_PROVIDER=tbank "
                f"требует заполненных настроек: {', '.join(missing_checkout)}."
            )

        if self.billing.payout_provider == "jump":
            missing_payout = [
                name
                for name, value in (
                    ("JUMP_ENABLED", "true" if self.jump.enabled else ""),
                    ("JUMP_API_KEY", self.jump.api_key),
                    ("JUMP_AGENT_ID", str(self.jump.agent_id or "")),
                )
                if not value
            ]
            if missing_payout:
                raise ValueError(
                    f"В окружении '{self.app_env}' BILLING_PAYOUT_PROVIDER=jump "
                    f"требует заполненных настроек: {', '.join(missing_payout)}."
                )
        return self


@lru_cache
def get_settings() -> Settings:
    """Возвращает закэшированный singleton настроек."""
    return Settings()


SettingsDep = Annotated[Settings, Depends(get_settings)]
"""FastAPI-аннотация для инъекции настроек."""
