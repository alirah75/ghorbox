import asyncio
import shutil

import pytest
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError, OperationalError

from app.core.security import Keyring, Purpose, Scope, ScopeCipher, SecurityError, create_keyring
from app.database.storage import Database, StorageError, prepare_storage, validate_storage


@pytest.fixture
async def storage_settings(settings, tmp_path):
    configured = settings.model_copy(
        update={"data_dir": tmp_path / "data", "secrets_dir": tmp_path / "secrets"}
    )
    create_keyring(configured.secrets_dir / "keyring.json")
    await prepare_storage(configured)
    return configured


async def test_three_independent_schemas_and_repeated_upgrade(storage_settings):
    await prepare_storage(storage_settings)
    await validate_storage(storage_settings)
    for kind in ("identity", "internal", "external"):
        db = Database(storage_settings.data_dir / f"{kind}.db", kind)
        try:
            async with db.transaction() as conn:
                tables = await conn.run_sync(lambda c: inspect(c).get_table_names())
                assert ("users" in tables) == (kind == "identity")
                assert ("external_people" in tables) == (kind == "external")
                assert ("messages" in tables) == (kind != "identity")
                assert (
                    await conn.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar() == "0001"
                assert (await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar() == 1
                assert (await conn.exec_driver_sql("PRAGMA journal_mode")).scalar() == "wal"
                assert (await conn.exec_driver_sql("PRAGMA synchronous")).scalar() == 2
        finally:
            await db.close()


async def test_unique_user_id_and_foreign_keys_enforced(storage_settings):
    db = Database(storage_settings.data_dir / "identity.db", "identity")
    users, admins = (db.metadata.tables[name] for name in ("users", "admins"))
    try:
        async with db.transaction() as conn:
            await conn.execute(users.insert().values(telegram_id=111, display_name="Ali"))
        with pytest.raises(IntegrityError):
            async with db.transaction() as conn:
                await conn.execute(users.insert().values(telegram_id=111, display_name="Duplicate"))
        with pytest.raises(IntegrityError):
            async with db.transaction() as conn:
                await conn.execute(admins.insert().values(user_id="does-not-exist"))
    finally:
        await db.close()


def conversation(cipher, id_="conversation"):
    return dict(
        id=id_,
        kind="suggestion",
        expires_at=2_000_000_000,
        subject_cipher=cipher.encrypt(
            b"recipient", record=id_, field="subject", purpose=Purpose.CONTENT
        ),
        route_cipher=cipher.encrypt(
            b"12345:67890", record=id_, field="route", purpose=Purpose.ROUTING
        ),
    )


def message(cipher, id_="message"):
    return dict(
        id=id_,
        conversation_id="conversation",
        sequence=1,
        message_type="initial",
        accepted_day="2026-09-17",
        body_cipher=cipher.encrypt(
            b"VERY PRIVATE FEEDBACK", record=id_, field="body", purpose=Purpose.CONTENT
        ),
        direction_cipher=cipher.encrypt(
            b"author-side-a", record=id_, field="direction", purpose=Purpose.ROUTING
        ),
    )


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_atomic_message_outbox_and_rollback(storage_settings, failure):
    cipher = ScopeCipher(
        Keyring.load(storage_settings.secrets_dir / "keyring.json"), Scope.INTERNAL
    )
    db = Database(storage_settings.data_dir / "internal.db", "internal")
    tables = db.metadata.tables
    try:
        with pytest.raises(failure):
            async with db.transaction() as conn:
                await conn.execute(tables["conversations"].insert().values(**conversation(cipher)))
                await conn.execute(tables["messages"].insert().values(**message(cipher)))
                await conn.execute(
                    tables["outbox"]
                    .insert()
                    .values(
                        message_id="message",
                        dedupe_key="test",
                        next_attempt_at=1,
                        destination_cipher=cipher.encrypt(
                            b"67890", record="job", field="dest", purpose=Purpose.ROUTING
                        ),
                    )
                )
                raise failure()
        async with db.transaction() as conn:
            for name in ("conversations", "messages", "outbox"):
                assert (
                    await conn.execute(select(func.count()).select_from(tables[name]))
                ).scalar() == 0
    finally:
        await db.close()


async def test_stored_content_is_encrypted_and_external_cannot_query_internal(storage_settings):
    cipher = ScopeCipher(
        Keyring.load(storage_settings.secrets_dir / "keyring.json"), Scope.INTERNAL
    )
    internal = Database(storage_settings.data_dir / "internal.db", "internal")
    external = Database(storage_settings.data_dir / "external.db", "external")
    try:
        async with internal.transaction() as conn:
            await conn.execute(
                internal.metadata.tables["conversations"].insert().values(**conversation(cipher))
            )
            await conn.execute(
                internal.metadata.tables["messages"].insert().values(**message(cipher))
            )
        async with external.transaction() as conn:
            assert (
                await conn.execute(
                    select(func.count()).select_from(external.metadata.tables["messages"])
                )
            ).scalar() == 0
        with pytest.raises(OperationalError):
            async with external.transaction() as conn:
                await conn.execute(text("SELECT * FROM users"))
    finally:
        await internal.close()
        await external.close()
    for path in storage_settings.data_dir.glob("*"):
        if path.is_file():
            content = path.read_bytes()
            assert b"VERY PRIVATE FEEDBACK" not in content
            assert b"12345:67890" not in content


async def test_wrong_keys_are_rejected_without_rebinding(storage_settings, tmp_path):
    alternate = tmp_path / "other" / "keyring.json"
    create_keyring(alternate)
    configured = storage_settings.model_copy(update={"secrets_dir": alternate.parent})
    for operation in (validate_storage, prepare_storage):
        with pytest.raises(SecurityError):
            await operation(configured)
    await validate_storage(storage_settings)


async def test_renamed_domain_database_is_rejected(storage_settings):
    shutil.copyfile(
        storage_settings.data_dir / "external.db", storage_settings.data_dir / "internal.db"
    )
    with pytest.raises(StorageError):
        await validate_storage(storage_settings)


async def test_missing_database_is_not_recreated_by_check(storage_settings):
    path = storage_settings.data_dir / "external.db"
    path.unlink()
    with pytest.raises(StorageError):
        await validate_storage(storage_settings)
    assert not path.exists()


async def test_missing_keys_are_not_generated(storage_settings):
    path = storage_settings.secrets_dir / "keyring.json"
    path.unlink()
    with pytest.raises(SecurityError):
        await prepare_storage(storage_settings)
    assert not path.exists()


async def test_failed_migration_rolls_back_ddl_and_version(settings, tmp_path, monkeypatch):
    from app.database import storage

    configured = settings.model_copy(
        update={"data_dir": tmp_path / "data", "secrets_dir": tmp_path / "keys"}
    )
    create_keyring(configured.secrets_dir / "keyring.json")
    original = storage._check

    def fail(*args):
        raise StorageError("simulated failure after DDL")

    monkeypatch.setattr(storage, "_check", fail)
    with pytest.raises(StorageError):
        await prepare_storage(configured)
    db = Database(configured.data_dir / "identity.db", "identity")
    try:
        async with db.transaction() as conn:
            assert await conn.run_sync(lambda c: inspect(c).get_table_names()) == []
    finally:
        await db.close()
    monkeypatch.setattr(storage, "_check", original)
    await prepare_storage(configured)
    await validate_storage(configured)


async def test_sql_parameters_keep_input_literal(storage_settings):
    db = Database(storage_settings.data_dir / "identity.db", "identity")
    users = db.metadata.tables["users"]
    attack = "Robert'); DROP TABLE users; --"
    try:
        async with db.transaction() as conn:
            await conn.execute(users.insert().values(telegram_id=333, display_name=attack))
        async with db.transaction() as conn:
            assert (await conn.execute(select(users.c.display_name))).scalar_one() == attack
    finally:
        await db.close()


async def test_commit_survives_reopen_and_invalid_outbox_is_rejected(storage_settings):
    cipher = ScopeCipher(
        Keyring.load(storage_settings.secrets_dir / "keyring.json"), Scope.INTERNAL
    )
    path = storage_settings.data_dir / "internal.db"
    db = Database(path, "internal")
    try:
        async with db.transaction() as conn:
            await conn.execute(
                db.metadata.tables["conversations"].insert().values(**conversation(cipher))
            )
            await conn.execute(db.metadata.tables["messages"].insert().values(**message(cipher)))
        with pytest.raises(IntegrityError):
            async with db.transaction() as conn:
                await conn.execute(
                    db.metadata.tables["outbox"]
                    .insert()
                    .values(destination_cipher=b"opaque", dedupe_key="bad", next_attempt_at=1)
                )
        with pytest.raises(IntegrityError):
            async with db.transaction() as conn:
                await conn.execute(
                    db.metadata.tables["messages"].insert().values(**message(cipher, "duplicate"))
                )
    finally:
        await db.close()
    reopened = Database(path, "internal")
    try:
        async with reopened.transaction() as conn:
            encrypted = (
                await conn.execute(select(reopened.metadata.tables["messages"].c.body_cipher))
            ).scalar_one()
            assert (
                cipher.decrypt(encrypted, record="message", field="body", purpose=Purpose.CONTENT)
                == b"VERY PRIVATE FEEDBACK"
            )
    finally:
        await reopened.close()


async def test_startup_refuses_missing_storage_before_telegram(settings, monkeypatch, tmp_path):
    from app.main import run_bot

    def forbidden(**kwargs):
        pytest.fail("Telegram must not be initialized before storage verification")

    monkeypatch.setattr("app.main.Bot", forbidden)
    configured = settings.model_copy(update={"secrets_dir": tmp_path / "missing"})
    with pytest.raises(SecurityError):
        await run_bot(configured)
