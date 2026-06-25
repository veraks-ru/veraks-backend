"""Юнит-тесты чистой хеш-цепочки аудита (детерминизм и связность)."""

from __future__ import annotations

from app.shared.audit.domain.hashing import canonical_json, chain_hash


def test_canonical_json_is_order_independent() -> None:
    """Канонический JSON не зависит от порядка ключей."""
    a = canonical_json({"b": 1, "a": 2})
    b = canonical_json({"a": 2, "b": 1})
    assert a == b == '{"a":2,"b":1}'


def test_chain_hash_is_deterministic() -> None:
    """Один и тот же вход даёт один и тот же хеш."""
    payload = {"action": "resolution.finalized", "outcome": True}
    assert chain_hash("prev", payload) == chain_hash("prev", payload)


def test_chain_hash_depends_on_prev_hash() -> None:
    """Изменение ``prev_hash`` меняет хеш звена (цепочка связна)."""
    payload = {"action": "x"}
    assert chain_hash(None, payload) != chain_hash("seed", payload)


def test_chain_hash_depends_on_payload() -> None:
    """Изменение содержимого меняет хеш (tamper-evident)."""
    assert chain_hash("p", {"outcome": True}) != chain_hash("p", {"outcome": False})


def test_chain_links_form_sequence() -> None:
    """Звенья связываются: hash(n) зависит от hash(n-1)."""
    h1 = chain_hash(None, {"n": 1})
    h2 = chain_hash(h1, {"n": 2})
    h2_tampered = chain_hash("forged", {"n": 2})
    assert h2 != h2_tampered
