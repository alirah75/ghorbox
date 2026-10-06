"""Authenticated encrypted offline snapshots and restore into a new directory only."""

import asyncio
import hashlib
import json
import os
import sqlite3
import struct
import tempfile
import time
import zipfile
from contextlib import ExitStack
from pathlib import Path
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import select

from app.core.runtime_lock import RuntimeLock
from app.core.security import (
    Keyring,
    Purpose,
    Scope,
    ScopeCipher,
    private_directory,
    validate_storage_paths,
)
from app.database.storage import KINDS, Database, validate_storage
from app.services.statistics_service import latest_period

MAGIC = b"AFB-BACKUP-1\n"
CHUNK = 1024 * 1024
FILES = {f"{kind}.db" for kind in KINDS}


class BackupError(Exception):
    """No raw archive, database or cryptographic errors should reach the CLI."""


async def file_work(function, *args):
    # Wait for file handles to close before TemporaryDirectory cleanup on cancellation.
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise


def absolute(path):
    return Path(path).absolute()


def check_stage(stage, parent):
    if stage.resolve().parent != parent.resolve():
        raise BackupError("Invalid staging path")


async def verify_content(settings):
    keyring = Keyring.load(settings.secrets_dir / "keyring.json")
    fields = {
        "conversations": [
            ("subject_cipher", "subject", Purpose.CONTENT),
            ("route_cipher", "route", Purpose.ROUTING),
        ],
        "messages": [
            ("body_cipher", "body", Purpose.CONTENT),
            ("direction_cipher", "direction", Purpose.ROUTING),
        ],
        "participant_access": [("permissions_cipher", "permissions", Purpose.ROUTING)],
        "outbox": [("destination_cipher", "destination", Purpose.ROUTING)],
        "weekly_reports": [("payload_cipher", "weekly_report", Purpose.CONTENT)],
        "interaction_sessions": [("state_cipher", "session", Purpose.ROUTING)],
    }
    for kind in ("internal", "external"):
        database = Database(settings.data_dir / f"{kind}.db", kind)
        cipher = ScopeCipher(keyring, Scope(kind))
        try:
            async with database.transaction() as conn:
                for name, columns in fields.items():
                    table = database.metadata.tables[name]
                    async with conn.stream(
                        select(table.c.id, *(table.c[col] for col, _, _ in columns))
                    ) as rows:
                        async for row in rows.mappings():
                            for column, field, purpose in columns:
                                json.loads(
                                    cipher.decrypt(
                                        row[column], record=row["id"], field=field, purpose=purpose
                                    )
                                )
        finally:
            await database.close()


def write_private(path, content):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def copy_private(source, destination):
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as dst, source.open("rb") as src:
        while chunk := src.read(CHUNK):
            dst.write(chunk)
        dst.flush()
        os.fsync(dst.fileno())


def recovery_floor(directory):
    path = directory / ".recovery.json"
    if not path.exists():
        return 0
    if path.is_symlink() or path.stat().st_size > 4096:
        raise BackupError("Invalid recovery state")
    value = json.loads(path.read_text("utf-8"))["statistics_floor"]
    if type(value) is not int or value < 0:
        raise BackupError("Invalid recovery state")
    return value


def _derive(keyring, version, salt):
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=b"anonymous-feedback/encrypted-backup/v1",
    ).derive(keyring.key(Scope.INTERNAL, Purpose.CONTENT, version))


def encrypt_archive(source, destination, keyring):
    salt, nonce = os.urandom(32), os.urandom(12)
    header = json.dumps(
        {"key_version": keyring.active, "salt": salt.hex(), "nonce": nonce.hex()},
        separators=(",", ":"),
    ).encode("ascii")
    aad = MAGIC + struct.pack(">I", len(header)) + header
    encryptor = Cipher(
        algorithms.AES(_derive(keyring, keyring.active, salt)), modes.GCM(nonce)
    ).encryptor()
    encryptor.authenticate_additional_data(aad)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as dst, source.open("rb") as src:
        dst.write(aad)
        while chunk := src.read(CHUNK):
            dst.write(encryptor.update(chunk))
        dst.write(encryptor.finalize())
        dst.write(encryptor.tag)
        dst.flush()
        os.fsync(dst.fileno())


def decrypt_archive(source, destination, keyring):
    if source.is_symlink() or not source.is_file():
        raise BackupError("Invalid archive")
    with source.open("rb") as src:
        magic = src.read(len(MAGIC))
        length_bytes = src.read(4)
        if magic != MAGIC or len(length_bytes) != 4:
            raise BackupError("Invalid archive")
        length = struct.unpack(">I", length_bytes)[0]
        if not 0 < length <= 4096:
            raise BackupError("Invalid header")
        raw = src.read(length)
        header = json.loads(raw)
        if set(header) != {"key_version", "salt", "nonce"}:
            raise BackupError("Invalid header")
        salt, nonce = bytes.fromhex(header["salt"]), bytes.fromhex(header["nonce"])
        if len(salt) != 32 or len(nonce) != 12:
            raise BackupError("Invalid header")
        begin = src.tell()
        src.seek(-16, 2)
        size = src.tell() - begin
        if size <= 0:
            raise BackupError("Invalid payload")
        tag = src.read(16)
        src.seek(begin)
        decryptor = Cipher(
            algorithms.AES(_derive(keyring, header["key_version"], salt)), modes.GCM(nonce, tag)
        ).decryptor()
        decryptor.authenticate_additional_data(magic + length_bytes + raw)
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as dst:
            while size:
                chunk = src.read(min(size, CHUNK))
                if not chunk:
                    raise BackupError("Incomplete payload")
                dst.write(decryptor.update(chunk))
                size -= len(chunk)
            dst.write(decryptor.finalize())  # Authentication must succeed before ZIP parsing.
            dst.flush()
            os.fsync(dst.fileno())


def snapshot(source, staging):
    # SQLite writer locks also protect against old releases without the runtime lock.
    with ExitStack() as stack:
        for kind in KINDS:
            conn = sqlite3.connect(
                (source / f"{kind}.db").resolve().as_uri() + "?mode=rw", uri=True, timeout=5
            )
            stack.callback(conn.close)
            conn.execute("BEGIN IMMEDIATE")
        for kind in KINDS:
            destination = staging / f"{kind}.db"
            write_private(destination, b"")
            reader = sqlite3.connect(
                (source / f"{kind}.db").resolve().as_uri() + "?mode=ro", uri=True
            )
            writer = sqlite3.connect(destination)
            try:
                reader.backup(writer)
                if writer.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    raise BackupError("Invalid snapshot")
            finally:
                writer.close()
                reader.close()


def pack(staging, archive, floor):
    manifest = {
        "format": 1,
        "schema": "0001",
        "created_at": int(time.time()),
        "statistics_floor": floor,
        "sha256": {name: digest(staging / name) for name in sorted(FILES)},
    }
    write_private(archive, b"")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as bundle:
        bundle.writestr("manifest.json", json.dumps(manifest))
        for name in sorted(FILES):
            bundle.write(staging / name, arcname=name)


def unpack(archive, staging):
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if len(entries) != 4 or {e.filename for e in entries} != FILES | {"manifest.json"}:
            raise BackupError("Invalid archive members")
        for entry in entries:
            if entry.compress_type != zipfile.ZIP_STORED or entry.file_size > 64 * 1024**3:
                raise BackupError("Unsupported archive member")
        if bundle.getinfo("manifest.json").file_size > 8192:
            raise BackupError("Invalid manifest")
        manifest = json.loads(bundle.read("manifest.json"))
        if (
            manifest["format"] != 1
            or manifest["schema"] != "0001"
            or set(manifest["sha256"]) != FILES
        ):
            raise BackupError("Unsupported manifest")
        floor = manifest.get("statistics_floor", 0)
        if type(floor) is not int or floor < 0:
            raise BackupError("Invalid recovery state")
        for name in sorted(FILES):
            destination = staging / name
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as dst, bundle.open(name) as src:
                while chunk := src.read(CHUNK):
                    dst.write(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            if digest(destination) != manifest["sha256"][name]:
                raise BackupError("Snapshot digest mismatch")
        return floor


def neutralize_delivery(staging):
    for kind in ("internal", "external"):
        conn = sqlite3.connect(staging / f"{kind}.db")
        conn.execute("PRAGMA secure_delete=ON")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                conn.execute(
                    "UPDATE outbox SET status='uncertain', lease_until=NULL, "
                    "error_code='restored_snapshot' WHERE status IN ('pending','sending')"
                )
                conn.execute("DELETE FROM interaction_sessions")
                conn.execute("UPDATE weekly_reports SET status='complete' WHERE status='queued'")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()


async def create_backup(settings, output):
    output = absolute(output)
    validate_storage_paths(output.parent, settings.secrets_dir)
    if output.exists() or output.is_symlink():
        raise BackupError("Backup destination already exists")
    private_directory(output.parent)
    with RuntimeLock(settings.data_dir):
        await validate_storage(settings)
        keyring = Keyring.load(settings.secrets_dir / "keyring.json")
        with tempfile.TemporaryDirectory(prefix=".backup-", dir=output.parent) as name:
            stage = Path(name)
            check_stage(stage, output.parent)
            await file_work(snapshot, settings.data_dir, stage)
            await validate_storage(settings.model_copy(update={"data_dir": stage}))
            await verify_content(settings.model_copy(update={"data_dir": stage}))
            archive, encrypted = stage / "snapshot.zip", stage / "snapshot.afb"
            await file_work(pack, stage, archive, recovery_floor(settings.data_dir))
            await file_work(encrypt_archive, archive, encrypted, keyring)
            # Exclusive creation also refuses destinations created by racing writers.
            await file_work(copy_private, encrypted, output)


async def restore_backup(settings, source, target=None):
    """target=None verifies in temporary storage; a target is always a brand-new directory."""
    source = absolute(source)
    target = absolute(target) if target is not None else None
    parent = target.parent if target else source.parent
    if (
        parent.resolve() == settings.secrets_dir.resolve()
        or settings.secrets_dir.resolve() in parent.resolve().parents
    ):
        raise BackupError("Restore staging must be separate from keys")
    if target is not None:
        validate_storage_paths(target, settings.secrets_dir)
        if target.exists() or target.is_symlink():
            raise BackupError("Restore target must not exist")
    private_directory(parent)
    keyring = Keyring.load(settings.secrets_dir / "keyring.json")
    with tempfile.TemporaryDirectory(prefix=".restore-", dir=parent) as name:
        stage = Path(name)
        check_stage(stage, parent)
        validate_storage_paths(stage, settings.secrets_dir)
        archive = stage / "snapshot.zip"
        await file_work(decrypt_archive, source, archive, keyring)
        floor = await file_work(unpack, archive, stage)
        recovered = settings.model_copy(update={"data_dir": stage})
        await validate_storage(recovered)
        await verify_content(recovered)
        if target is None:
            return
        await file_work(neutralize_delivery, stage)
        await validate_storage(recovered)
        target.mkdir(mode=0o700, exist_ok=False)
        marker = target / ".restore-incomplete"
        write_private(marker, b"Restore not yet published")
        with RuntimeLock(target):
            for file in sorted(FILES):
                await file_work(copy_private, stage / file, target / file)
            floor = max(floor, latest_period(time.time(), ZoneInfo(settings.app_timezone))[1])
            write_private(
                target / ".recovery.json", json.dumps({"statistics_floor": floor}).encode()
            )
            # A crash leaves the marker; startup refuses partial restoration.
            await validate_storage(
                settings.model_copy(update={"data_dir": target}), allow_incomplete=True
            )
            marker.unlink()
