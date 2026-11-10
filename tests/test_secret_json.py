"""Per-key Fernet encryption for the ``SecretJSON`` column type, plus the prod key check."""

from __future__ import annotations

import os

import pytest
from cryptography.fernet import Fernet

from bifrost.cli.config import BifrostSettings
from bifrost.core.crypto import (
    CIPHERTEXT_PREFIX,
    RESERVED_PREFIX,
    CredentialKeyError,
    SecretJSON,
)

KEY_A = Fernet.generate_key().decode()
KEY_B = Fernet.generate_key().decode()
SECRET_KEY = f"{RESERVED_PREFIX}token"


def _col(keys: str | None) -> SecretJSON:
    return SecretJSON(keys=lambda: keys)


def test_reserved_value_is_stored_as_prefixed_ciphertext() -> None:
    stored = _col(KEY_A).process_bind_param({SECRET_KEY: "hunter2", "squad": "x"}, None)
    assert stored[SECRET_KEY].startswith(CIPHERTEXT_PREFIX)
    assert "hunter2" not in stored[SECRET_KEY]


def test_round_trip_returns_plaintext() -> None:
    col = _col(KEY_A)
    stored = col.process_bind_param({SECRET_KEY: "hunter2"}, None)
    assert col.process_result_value(stored, None) == {SECRET_KEY: "hunter2"}


def test_non_reserved_keys_are_untouched() -> None:
    col = _col(KEY_A)
    value = {"squad": "orders", "token_hint": "not reserved", "n": {"a": 1}}
    assert col.process_bind_param(value, None) == value
    assert col.process_result_value(value, None) == value


def test_bind_does_not_mutate_the_callers_dict() -> None:
    value = {SECRET_KEY: "hunter2"}
    _col(KEY_A).process_bind_param(value, None)
    assert value == {SECRET_KEY: "hunter2"}


def test_none_passes_through() -> None:
    col = _col(KEY_A)
    assert col.process_bind_param(None, None) is None
    assert col.process_result_value(None, None) is None


def test_rotation_decrypts_data_written_with_the_old_key() -> None:
    stored = _col(KEY_A).process_bind_param({SECRET_KEY: "hunter2"}, None)
    rotated = _col(f"{KEY_B},{KEY_A}")
    assert rotated.process_result_value(stored, None) == {SECRET_KEY: "hunter2"}


def test_rotation_encrypts_with_the_first_key() -> None:
    stored = _col(f"{KEY_B},{KEY_A}").process_bind_param({SECRET_KEY: "hunter2"}, None)
    assert _col(KEY_B).process_result_value(stored, None) == {SECRET_KEY: "hunter2"}
    with pytest.raises(CredentialKeyError):
        _col(KEY_A).process_result_value(stored, None)


def test_legacy_plaintext_reads_back_and_is_encrypted_on_next_write() -> None:
    col = _col(KEY_A)
    legacy = {SECRET_KEY: "old-plaintext"}
    read = col.process_result_value(legacy, None)
    assert read == legacy
    assert col.process_bind_param(read, None)[SECRET_KEY].startswith(CIPHERTEXT_PREFIX)


def test_missing_key_refuses_reserved_value_at_bind() -> None:
    for keys in (None, ""):
        with pytest.raises(CredentialKeyError, match="BIFROST_CREDENTIAL_KEY"):
            _col(keys).process_bind_param({SECRET_KEY: "hunter2"}, None)


def test_missing_key_is_fine_without_reserved_values() -> None:
    assert _col(None).process_bind_param({"squad": "x"}, None) == {"squad": "x"}


def test_missing_key_cannot_read_ciphertext() -> None:
    stored = _col(KEY_A).process_bind_param({SECRET_KEY: "hunter2"}, None)
    with pytest.raises(CredentialKeyError, match="BIFROST_CREDENTIAL_KEY"):
        _col(None).process_result_value(stored, None)


def test_non_string_reserved_value_is_rejected() -> None:
    with pytest.raises(TypeError, match=SECRET_KEY):
        _col(KEY_A).process_bind_param({SECRET_KEY: {"nested": 1}}, None)


def test_default_key_comes_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from bifrost.cli.config import reset_settings

    monkeypatch.setenv("BIFROST_CREDENTIAL_KEY", KEY_A)
    reset_settings()
    try:
        col = SecretJSON()
        stored = col.process_bind_param({SECRET_KEY: "hunter2"}, None)
        assert col.process_result_value(stored, None) == {SECRET_KEY: "hunter2"}
    finally:
        monkeypatch.undo()
        reset_settings()


class TestProductionCredentialKey:
    @pytest.fixture(autouse=True)
    def _isolate_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key in [k for k in os.environ if k.startswith("BIFROST_")]:
            monkeypatch.delenv(key, raising=False)

    def _errors(self, **overrides: object) -> list[str]:
        settings = BifrostSettings(_env_file=None, production_mode=True, **overrides)  # type: ignore[call-arg]
        return settings.validate_production_secrets()

    def test_empty_credential_key_fails_in_production(self) -> None:
        assert any("BIFROST_CREDENTIAL_KEY" in e for e in self._errors())

    def test_set_credential_key_passes_that_check(self) -> None:
        assert not any("BIFROST_CREDENTIAL_KEY" in e for e in self._errors(credential_key=KEY_A))

    def test_not_required_outside_production(self) -> None:
        settings = BifrostSettings(_env_file=None, production_mode=False)  # type: ignore[call-arg]
        assert settings.validate_production_secrets() == []
