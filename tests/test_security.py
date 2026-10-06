import json

import pytest

from app.core.security import (
    Keyring,
    Purpose,
    Scope,
    ScopeCipher,
    SecurityError,
    create_keyring,
    validate_storage_paths,
)


@pytest.fixture
def keyring(tmp_path):
    path = tmp_path / "secrets" / "keyring.json"
    create_keyring(path)
    return Keyring.load(path)


@pytest.mark.parametrize("scope", list(Scope))
@pytest.mark.parametrize("purpose", [Purpose.CONTENT, Purpose.ROUTING])
def test_authenticated_roundtrip_and_random_nonce(keyring, scope, purpose):
    cipher = ScopeCipher(keyring, scope)
    data = "متن محرمانه و شناسه ۱۲۳۴".encode()
    args = {"record": "record-1", "field": "body", "purpose": purpose}
    first, second = cipher.encrypt(data, **args), cipher.encrypt(data, **args)
    assert first != second
    assert data not in first
    assert cipher.decrypt(first, **args) == data


@pytest.mark.parametrize("change", ["scope", "purpose", "record", "field", "payload", "version"])
def test_substitution_and_tampering_fail_closed(keyring, change):
    cipher = ScopeCipher(keyring, Scope.INTERNAL)
    args = {"record": "one", "field": "body", "purpose": Purpose.CONTENT}
    encrypted = cipher.encrypt(b"private content", **args)
    if change == "scope":
        cipher = ScopeCipher(keyring, Scope.EXTERNAL)
    elif change == "purpose":
        args["purpose"] = Purpose.ROUTING
    elif change in {"record", "field"}:
        args[change] = "other"
    elif change == "payload":
        encrypted = encrypted[:-1] + bytes([encrypted[-1] ^ 1])
    else:
        encrypted = encrypted.replace(b":v1:", b":v2:", 1)
    with pytest.raises(SecurityError) as error:
        cipher.decrypt(encrypted, **args)
    assert "private content" not in str(error.value)


@pytest.mark.parametrize("data", [b"", b"AFB1:v1:", b"AFB1:\xff:invalid", b"plaintext"])
def test_malformed_ciphertext_is_safe(keyring, data):
    with pytest.raises(SecurityError):
        ScopeCipher(keyring, Scope.INTERNAL).decrypt(
            data, record="one", field="body", purpose=Purpose.CONTENT
        )


def test_tags_are_contextual_and_conversation_specific(keyring):
    internal, external = (ScopeCipher(keyring, scope) for scope in Scope)
    original = internal.tag("participant", "conversation-a", "123")
    assert original == internal.tag("participant", "conversation-a", "123")
    assert original != internal.tag("participant", "conversation-b", "123")
    assert original != external.tag("participant", "conversation-a", "123")
    assert original != internal.tag("reporter", "conversation-a", "123")
    assert internal.tag("x", "ab", "c") != internal.tag("x", "a", "bc")


def test_keys_are_distinct_and_not_in_repr(keyring):
    keys = [keyring.key(scope, purpose, "v1") for scope in Scope for purpose in Purpose]
    assert len(set(keys)) == 6
    assert all(len(key) == 32 for key in keys)
    assert repr(keyring) == "Keyring(<redacted>)"


def test_existing_key_file_is_not_replaced(tmp_path):
    path = tmp_path / "secrets" / "keyring.json"
    create_keyring(path)
    previous = path.read_bytes()
    with pytest.raises(FileExistsError):
        create_keyring(path)
    assert path.read_bytes() == previous


@pytest.mark.parametrize("bad", ["invalid-json-secret", "{}", "[]", '{"format":99}'])
def test_invalid_key_file_does_not_expose_secrets(tmp_path, bad):
    path = tmp_path / "keyring.json"
    path.write_text(bad, encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(SecurityError) as error:
        Keyring.load(path)
    assert bad not in str(error.value)


def test_duplicate_key_material_rejected(tmp_path):
    path = tmp_path / "secrets" / "keyring.json"
    create_keyring(path)
    document = json.loads(path.read_text())
    keys = document["versions"]["v1"]
    keys["external.routing"] = keys["internal.routing"]
    path.write_text(json.dumps(document))
    with pytest.raises(SecurityError):
        Keyring.load(path)


@pytest.mark.parametrize(
    "data,secrets", [("same", "same"), ("root", "root/keys"), ("root/db", "root")]
)
def test_secrets_cannot_be_inside_data_directory(tmp_path, data, secrets):
    with pytest.raises(SecurityError):
        validate_storage_paths(tmp_path / data, tmp_path / secrets)
