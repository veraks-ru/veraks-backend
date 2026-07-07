"""Настройки эквайринга ТБанк (TBankSettings) — загрузка из env."""

from app.config import TBankSettings


def test_tbank_settings_defaults_and_env(monkeypatch):
    monkeypatch.setenv("TBANK_TERMINAL_KEY", "1783427792728DEMO")
    monkeypatch.setenv("TBANK_PASSWORD", "secret")
    s = TBankSettings()
    assert s.terminal_key == "1783427792728DEMO"
    assert s.password == "secret"
    assert s.api_base_url == "https://securepay.tinkoff.ru/v2"
    assert s.taxation == "usn_income"
    assert s.enabled is False
