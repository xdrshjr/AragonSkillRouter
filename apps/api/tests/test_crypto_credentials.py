"""加解密、掩码、轮换、无明文泄漏（§11.2 / §15.3）。"""

from __future__ import annotations

import pytest

from skillrouter_api.crypto import secretbox
from skillrouter_api.crypto.masterkey import fingerprint, master_key
from skillrouter_api.security.passwords import hash_password, validate_strength, verify_password


def test_seal_open_roundtrip() -> None:
    sealed = secretbox.seal("sk-super-secret-value", record_type="credential", record_id="crd_1")
    assert sealed.startswith("v1.")
    assert "sk-super-secret-value" not in sealed
    assert secretbox.open_sealed(sealed, record_type="credential", record_id="crd_1") == "sk-super-secret-value"


def test_aad_binding_rejects_wrong_record() -> None:
    sealed = secretbox.seal("abc", record_type="credential", record_id="crd_1")
    with pytest.raises(Exception):
        secretbox.open_sealed(sealed, record_type="credential", record_id="crd_2")


def test_nonce_is_random() -> None:
    a = secretbox.seal("same", record_type="credential", record_id="crd_1")
    b = secretbox.seal("same", record_type="credential", record_id="crd_1")
    assert a != b


def test_fingerprint_stable() -> None:
    assert fingerprint() == fingerprint(master_key())
    assert len(fingerprint()) == 16


def test_credential_masked_in_api(client, admin, provider) -> None:
    r = client.post(
        f"/api/admin/providers/{provider['id']}/credentials",
        headers=admin["headers"],
        json={"name": "泄漏检查", "kind": "llm", "auth_type": "api_key", "secret": "sk-leak-check-abcdef123456"},
    )
    assert r.status_code == 201
    assert "sk-leak-check-abcdef123456" not in r.text
    masked = r.json()["secret"]["masked"]
    assert masked.endswith("3456") and "***" in masked


def test_ciphertext_never_returned(client, admin, provider) -> None:
    r = client.post(
        f"/api/admin/providers/{provider['id']}/credentials",
        headers=admin["headers"],
        json={"name": "密文检查", "kind": "llm", "auth_type": "bearer", "secret": "sk-cipher-xyz"},
    )
    credential_id = r.json()["id"]
    detail = client.get(f"/api/admin/credentials/{credential_id}")
    assert "secret_ciphertext" not in detail.text
    assert detail.json()["secret"]["masked"].startswith("sk-")


def test_replace_and_rotate(client, admin, provider) -> None:
    r = client.post(
        f"/api/admin/providers/{provider['id']}/credentials",
        headers=admin["headers"],
        json={"name": "轮换源", "kind": "llm", "auth_type": "bearer", "secret": "sk-old-value-1"},
    )
    credential_id = r.json()["id"]
    r = client.post(
        f"/api/admin/credentials/{credential_id}/replace",
        headers=admin["headers"],
        json={"secret": "sk-new-value-2"},
    )
    assert r.status_code == 200
    assert r.json()["secret"]["masked"].endswith("ue-2")
    r = client.post(
        f"/api/admin/credentials/{credential_id}/rotate",
        headers=admin["headers"],
        json={"secret": "sk-rotated-value-3", "name": "轮换新凭证"},
    )
    assert r.status_code == 200
    assert r.json()["rotation_of"] == credential_id
    old = client.get(f"/api/admin/credentials/{credential_id}")
    assert old.json()["status"] == "disabled"


def test_password_hash_and_strength() -> None:
    hashed = hash_password("SkillRouter-Test-2026")
    assert hashed.startswith("$argon2")
    assert verify_password("SkillRouter-Test-2026", hashed)
    assert not verify_password("wrong", hashed)
    assert validate_strength("short") != []
    assert validate_strength("adminjr") != []
    assert validate_strength("LongEnough123") == []
