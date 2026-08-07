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


_KNOWN_AUTH_PROVIDERS = ("esia", "email")


class AuthSettings(BaseSettings):
    """Какие способы входа включены в этой инсталляции (``AUTH_PROVIDERS``).

    Список через запятую из ``esia`` и/или ``email``. Разбор — по образцу
    ``EsiaSettings.scope_list``.

    Дефолт — только ``email``: договор с интегратором ЕСИА ещё не заключён,
    а запускаться нужно сейчас. Код ЕСИА при этом не удаляется — провайдер
    возвращается одной переменной окружения, когда договор появится.

    Выключенный провайдер означает, что его эндпоинты недоступны (404, см.
    ``identity.api.dependencies.require_esia_provider``), а его настройки не
    обязательны к заполнению (см. ``Settings._require_esia_when_enabled``).
    """

    model_config = SettingsConfigDict(env_prefix="AUTH_", extra="ignore")

    providers: str = "email"

    @property
    def provider_set(self) -> frozenset[str]:
        """Множество включённых провайдеров (нормализованное)."""
        return frozenset(
            item.strip().lower() for item in self.providers.split(",") if item.strip()
        )

    @property
    def esia_enabled(self) -> bool:
        """Включён ли вход через Госуслуги."""
        return "esia" in self.provider_set

    @property
    def email_enabled(self) -> bool:
        """Включён ли вход по email и одноразовой ссылке."""
        return "email" in self.provider_set

    @model_validator(mode="after")
    def _validate_providers(self) -> AuthSettings:
        """Хотя бы один известный провайдер; опечатки — ошибка старта.

        Без этой проверки опечатка (``AUTH_PROVIDERS=emails``) молча оставила
        бы платформу вообще без способа входа, и обнаружилось бы это только
        первым пользователем.
        """
        providers = self.provider_set
        unknown = sorted(providers - set(_KNOWN_AUTH_PROVIDERS))
        if unknown:
            raise ValueError(
                f"Неизвестные провайдеры входа в AUTH_PROVIDERS: {', '.join(unknown)}. "
                f"Допустимые значения: {', '.join(_KNOWN_AUTH_PROVIDERS)}."
            )
        if not providers:
            raise ValueError(
                "AUTH_PROVIDERS не может быть пустым: платформа осталась бы без "
                f"единственного способа входа. Допустимо: {', '.join(_KNOWN_AUTH_PROVIDERS)}."
            )
        return self


class MailSettings(BaseSettings):
    """Отправка писем (ссылка входа по email).

    ``host`` пуст = SMTP не настроен. Это НЕ валит старт приложения: выбор
    адаптера идёт по факту настройки (см.
    ``app.shared.mail.adapters.factory.build_email_sender``) — с пустым
    ``MAIL_HOST`` письма пишутся в лог. Обоснование такой деградации вместо
    fail-fast — в докстринге фабрики.

    ``link_base_url`` — базовый URL фронта, от которого строится ссылка входа
    (``<link_base_url>/auth/email/callback?token=…``).
    """

    model_config = SettingsConfigDict(env_prefix="MAIL_", extra="ignore")

    host: str = ""
    port: int = Field(default=587, ge=1, le=65535)
    # Логин/пароль опциональны: локальный mailpit принимает почту без них.
    username: str = ""
    password: str = ""
    # Совпадает с infra/helm values.yaml (mailFromAddress): именно на этот
    # адрес настраиваются SPF/DKIM, расхождение дефолта с боевым значением
    # означало бы, что письма из ненастроенного окружения летят «не оттуда».
    from_address: str = "noreply@veraks.ru"
    from_name: str = "Веракс"
    # Прямое TLS-соединение (обычно порт 465).
    use_tls: bool = False
    # Апгрейд открытого соединения командой STARTTLS (обычно порт 587).
    use_starttls: bool = True
    link_base_url: str = "https://veraks.ru"

    @property
    def configured(self) -> bool:
        """Задан ли SMTP-хост (иначе письма уходят в лог)."""
        return bool(self.host.strip())

    @property
    def sender_header(self) -> str:
        """Значение заголовка ``From``: ``Имя <адрес>``."""
        return f"{self.from_name} <{self.from_address}>"


class EsiaSettings(BaseSettings):
    """Параметры подключения к ЕСИА (через сертифицированный шлюз).

    ``jwks_url``/``issuer`` включают полноценную проверку ``id_token``
    (подпись по JWKS + ``iss``/``aud``/``exp``/``nonce``). Пустой ``jwks_url``
    — режим «доверие каналу»: id_token не проверяется, что допустимо только
    локально с моком (вне ``local`` пустое значение валит старт приложения,
    см. ``Settings._require_esia_id_token_verification``).

    Все поля имеют пустые дефолты: при выключенном провайдере ``esia``
    (``AUTH_PROVIDERS`` без ``esia``) приложение обязано подниматься вообще
    без переменных ``ESIA_*``. Обязательность возвращается валидатором
    ``Settings._require_esia_when_enabled`` — но только когда ЕСИА включена.
    """

    model_config = SettingsConfigDict(env_prefix="ESIA_", extra="ignore")

    client_id: str = ""
    redirect_uri: str = ""
    authorization_endpoint: str = ""
    token_endpoint: str = ""
    userinfo_endpoint: str = ""
    scopes: str = "openid snils fullname"
    # Требовать «подтверждённую» учётную запись ЕСИА (отклонять упрощённую/стандартную).
    require_confirmed: bool = True

    # JWKS шлюза (публичные ключи подписи id_token) и ожидаемый ``iss``.
    jwks_url: str = ""
    issuer: str = ""
    # Кэш ключей в памяти адаптера: сколько секунд не ходить за JWKS повторно.
    # Ротация ключа «вне расписания» ловится принудительным обновлением по
    # неизвестному ``kid`` (см. ``EsiaIdTokenVerifier``).
    jwks_cache_ttl_seconds: int = Field(default=3600, ge=1)
    # Допустимые алгоритмы подписи id_token (крипто-гибкость без правки кода:
    # разные интеграторы подписывают RS256/PS256/ES256).
    id_token_algorithms: str = "RS256"

    @property
    def scope_list(self) -> list[str]:
        """Список scope'ов из строки, разделённой пробелами."""
        return self.scopes.split()

    @property
    def id_token_algorithm_list(self) -> list[str]:
        """Список допустимых алгоритмов подписи id_token."""
        return self.id_token_algorithms.split()

    @property
    def verify_id_token(self) -> bool:
        """Включена ли криптографическая проверка ``id_token``."""
        return bool(self.jwks_url)


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

    # Отдельный, более жёсткий лимит для путей аутентификации (/auth/*:
    # инициация ЕСИА, callback, refresh) — это точка входа для брутфорса
    # подбора состояний/токенов и скрейпинга логина, а не обычная витрина,
    # поэтому общий лимит (300/мин) для неё слишком мягкий. 20/мин с одного
    # IP с запасом покрывает ручные повторные заходы живого пользователя
    # (проблемы с ЕСИА, случайный дубль-клик), но душит автоматический перебор.
    # 0 — выключено (используется только общий лимит).
    rate_limit_auth_per_minute: int = Field(default=20, ge=0)

    # Авто-финализация сезонов в таймерном ``season_roll``. Включена: боевой
    # ``ResolutionDisputeGuard`` (домен resolutions) блокирует финализацию сезона
    # с открытыми спорами, поэтому таймерное авто-закрытие безопасно (§6.4/§6.5).
    # Выключить можно через env, если нужно временно перевести на ручной режим.
    seasons_auto_finalize: bool = True

    security: SecuritySettings = Field(default_factory=SecuritySettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    mail: MailSettings = Field(default_factory=MailSettings)
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
    def _require_billing_providers_in_prod(self) -> Settings:
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

    @model_validator(mode="after")
    def _require_esia_when_enabled(self) -> Settings:
        """Настройки ЕСИА обязательны ровно тогда, когда провайдер включён.

        Раньше ``EsiaSettings`` были обязательным вложенным полем без
        дефолтов: без ``ESIA_*`` приложение не стартовало вовсе. Пока договор
        с интегратором не заключён, ЕСИА выключена (``AUTH_PROVIDERS=email``),
        и требовать её реквизиты — значит держать в проде фиктивные значения,
        неотличимые от настоящих. Поэтому обязательность привязана к факту
        включения провайдера, а не к самому существованию кода ЕСИА.
        """
        if not self.auth.esia_enabled:
            return self
        missing = [
            name
            for name, value in (
                ("ESIA_CLIENT_ID", self.esia.client_id),
                ("ESIA_REDIRECT_URI", self.esia.redirect_uri),
                ("ESIA_AUTHORIZATION_ENDPOINT", self.esia.authorization_endpoint),
                ("ESIA_TOKEN_ENDPOINT", self.esia.token_endpoint),
                ("ESIA_USERINFO_ENDPOINT", self.esia.userinfo_endpoint),
            )
            if not value.strip()
        ]
        if missing:
            raise ValueError(
                "AUTH_PROVIDERS включает 'esia', поэтому обязательны настройки: "
                f"{', '.join(missing)}."
            )
        return self

    @model_validator(mode="after")
    def _require_esia_id_token_verification(self) -> Settings:
        """Вне ``local`` ``id_token`` ЕСИА обязан проверяться криптографически.

        Тот же паттерн fail-fast, что и у платёжных провайдеров выше. Без
        ``ESIA_JWKS_URL`` адаптер работает в режиме «доверие каналу»: маркер
        принимается без проверки подписи, и подмена ответа token-эндпоинта
        (или скомпрометированный шлюз) даёт вход под чужой учётной записью.
        Локально это осознанный компромисс ради мока, в бою — недопустимо,
        поэтому приложение не поднимется.

        ``ESIA_ISSUER`` проверяется НЕ по окружению, а по включённости
        проверки: с заданным JWKS и пустым ``issuer`` ни один маркер не
        пройдёт (``iss`` сверяется с пустой строкой), и локально это
        выглядело бы как невнятная ошибка входа вместо явной ошибки
        конфигурации.

        Вся проверка применяется только при включённом провайдере ``esia``:
        с выключенной ЕСИА её эндпоинты недоступны (404), и требовать от
        оператора корректный JWKS для мёртвого кода не за что.
        """
        if not self.auth.esia_enabled:
            return self
        if self.esia.jwks_url.strip() and not self.esia.issuer.strip():
            raise ValueError(
                "ESIA_ISSUER обязателен, если задан ESIA_JWKS_URL: без "
                "ожидаемого iss ни один id_token не пройдёт проверку."
            )
        if self.app_env == "local":
            return self
        if not self.esia.jwks_url.strip():
            raise ValueError(
                f"В окружении '{self.app_env}' обязательна проверка id_token ЕСИА: "
                "заполните ESIA_JWKS_URL (пустой = режим «доверие каналу», "
                "допустим только в local)."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Возвращает закэшированный singleton настроек."""
    return Settings()


SettingsDep = Annotated[Settings, Depends(get_settings)]
"""FastAPI-аннотация для инъекции настроек."""
