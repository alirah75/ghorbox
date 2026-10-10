"""Explicit initialization, migration, integrity checks and per-file transactions."""

import hmac
import os
import stat
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import URL, event, inspect, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings
from app.core.security import (
    Keyring,
    Purpose,
    Scope,
    ScopeCipher,
    SecurityError,
    private_directory,
    validate_storage_paths,
)
from app.database.schema import schema

KINDS = ("identity", "internal", "external")
REVISION = "0002"
PROBE = b"anonymous-feedback-storage-binding-v1"


class StorageError(Exception):
    """Safe storage exception; never include raw SQL errors in user output."""


class Database:
    def __init__(self, path: Path, kind: str):
        if kind not in KINDS or path.is_symlink() or not path.is_file():
            raise StorageError("Storage is not initialized or its path is invalid.")
        if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise StorageError("Private database file permissions are required.")
        self.kind = kind
        self.path = path
        self.metadata = schema(kind)
        # mode=rw refuses silent recreation if a database disappears.
        url = URL.create(
            "sqlite+aiosqlite",
            database=path.resolve().as_uri(),
            query={"mode": "rw", "uri": "true"},
        )
        self.engine = create_async_engine(
            url,
            echo=False,
            hide_parameters=True,
            pool_size=1,
            max_overflow=0,
            connect_args={"timeout": 5},
        )

        @event.listens_for(self.engine.sync_engine, "connect")
        def configure_connection(connection, _record):
            connection.isolation_level = None
            cursor = connection.cursor()
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=FULL")
            cursor.execute("PRAGMA secure_delete=ON")
            cursor.close()

        @event.listens_for(self.engine.sync_engine, "begin")
        def explicit_begin(connection):
            statement = (
                "BEGIN IMMEDIATE" if connection.get_execution_options().get("write") else "BEGIN"
            )
            connection.exec_driver_sql(statement)

    @asynccontextmanager
    async def transaction(self, *, write=False):
        """Commits only on success; cancellation and exceptions roll everything back."""
        async with self.engine.connect() as connection:
            connection = await connection.execution_options(write=write)
            async with connection.begin():
                yield connection

    async def close(self):
        await self.engine.dispose()


def _tables(connection) -> set[str]:
    return set(inspect(connection).get_table_names())


def _identity(connection, kind):
    table = schema(kind).tables["database_identity"]
    rows = connection.execute(select(table.c.kind, table.c.instance_id)).all()
    if len(rows) != 1 or rows[0].kind != kind:
        raise StorageError("Database scope does not match its configured file.")
    return rows[0].instance_id


def _verify_binding(connection, kind, keyring):
    instance_id = _identity(connection, kind)
    if kind == "identity":
        return
    table = schema(kind).tables["key_bindings"]
    rows = connection.execute(select(table)).mappings().all()
    if not rows or keyring.active not in {row["key_version"] for row in rows}:
        raise SecurityError("Storage keys do not match; automatic replacement is forbidden.")
    cipher = ScopeCipher(keyring, Scope(kind))
    for row in rows:
        for purpose in (Purpose.CONTENT, Purpose.ROUTING):
            check = cipher.decrypt(
                row[f"{purpose}_check"], record=instance_id, field="key-binding", purpose=purpose
            )
            if not hmac.compare_digest(check, PROBE):
                raise SecurityError("Storage key verification failed.")
        expected = cipher.tag("key-binding", instance_id, version=row["key_version"])
        if not hmac.compare_digest(row["index_check"], expected):
            raise SecurityError("Storage key verification failed.")


def _bind_new_database(connection, kind, keyring):
    if kind == "identity":
        return
    instance_id = _identity(connection, kind)
    cipher = ScopeCipher(keyring, Scope(kind))
    table = schema(kind).tables["key_bindings"]
    connection.execute(
        table.insert().values(
            key_version=keyring.active,
            content_check=cipher.encrypt(
                PROBE, record=instance_id, field="key-binding", purpose=Purpose.CONTENT
            ),
            routing_check=cipher.encrypt(
                PROBE, record=instance_id, field="key-binding", purpose=Purpose.ROUTING
            ),
            index_check=cipher.tag("key-binding", instance_id),
        )
    )


def _upgrade(connection, kind, keyring):
    existing = _tables(connection)
    if existing:
        if "database_identity" not in existing or "alembic_version" not in existing:
            raise StorageError("Unknown database; refusing to modify it.")
        _verify_binding(connection, kind, keyring)
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    config.attributes.update(connection=connection, kind=kind)
    command.upgrade(config, "head")
    if not existing:
        _bind_new_database(connection, kind, keyring)


def _check(connection, kind, keyring):
    metadata = schema(kind)
    if _tables(connection) != set(metadata.tables) | {"alembic_version"}:
        raise StorageError("Database tables do not match the supported schema.")
    if connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() != REVISION:
        raise StorageError("Database migration is required.")
    inspector = inspect(connection)
    for name, table in metadata.tables.items():
        if {column["name"] for column in inspector.get_columns(name)} != set(table.c.keys()):
            raise StorageError("Database columns do not match the supported schema.")
    _verify_binding(connection, kind, keyring)
    if connection.exec_driver_sql("PRAGMA quick_check").scalar_one() != "ok":
        raise StorageError("Database integrity check failed.")
    if connection.exec_driver_sql("PRAGMA foreign_key_check").all():
        raise StorageError("Database references are inconsistent.")
    for pragma, expected in (("foreign_keys", 1), ("journal_mode", "wal"), ("synchronous", 2)):
        if connection.exec_driver_sql(f"PRAGMA {pragma}").scalar_one() != expected:
            raise StorageError("Database safety settings are not active.")


async def prepare_storage(settings: Settings) -> None:
    if (settings.data_dir / ".restore-incomplete").exists():
        raise StorageError("Incomplete restore must not be migrated.")
    validate_storage_paths(settings.data_dir, settings.secrets_dir)
    keyring = Keyring.load(settings.secrets_dir / "keyring.json")
    private_directory(settings.data_dir)
    # Migrations are atomic per file, not across files. Retrying resumes safely.
    for kind in KINDS:
        path = settings.data_dir / f"{kind}.db"
        if not path.exists():
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
        database = Database(path, kind)
        try:
            async with database.transaction() as connection:
                await connection.run_sync(_upgrade, kind, keyring)
                await connection.run_sync(_check, kind, keyring)
        finally:
            await database.close()


async def validate_storage(settings: Settings, *, allow_incomplete=False) -> None:
    if not allow_incomplete and (settings.data_dir / ".restore-incomplete").exists():
        raise StorageError("Incomplete restore must not be opened.")
    validate_storage_paths(settings.data_dir, settings.secrets_dir)
    keyring = Keyring.load(settings.secrets_dir / "keyring.json")
    for kind in KINDS:
        database = Database(settings.data_dir / f"{kind}.db", kind)
        try:
            async with database.transaction() as connection:
                await connection.run_sync(_check, kind, keyring)
        finally:
            await database.close()
