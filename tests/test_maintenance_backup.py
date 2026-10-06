import asyncio
import threading
import zipfile
from unittest.mock import AsyncMock

import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy import select, update

from app.core.runtime_lock import RuntimeBusy, RuntimeLock
from app.core.security import Keyring, Scope, ScopeCipher, SecurityError, create_keyring
from app.database.storage import Database, StorageError, validate_storage
from app.services.backup_service import (
    BackupError,
    create_backup,
    encrypt_archive,
    file_work,
    restore_backup,
)
from app.services.conversation_service import ConversationService
from app.services.feedback_service import FeedbackService
from app.services.identity_service import IdentityService
from app.services.maintenance_service import MaintenanceService
from app.services.statistics_service import StatisticsService
from app.workers.delivery import DeliveryWorker
from tests.test_conversations import internal_conversation
from tests.test_feedback import BODY, count
from tests.test_moderation import action, reported


def configured(settings, tmp_path):
    return settings.model_copy(
        update={"data_dir": tmp_path / "data", "secrets_dir": tmp_path / "keys"}
    )


async def test_backup_restore_content_and_no_resend(settings, feedback_service, tmp_path):
    s = feedback_service
    cid = await internal_conversation(s)
    await StatisticsService(s).tick()
    config = configured(settings, tmp_path)
    archive = tmp_path / "backups" / "first.afb"
    await create_backup(config, archive)
    raw = archive.read_bytes()
    assert BODY.encode() not in raw and b"SQLite format" not in raw
    assert b"telegram_id" not in raw and b"keyring" not in raw
    await restore_backup(config, archive)
    destination = tmp_path / "restored"
    await restore_backup(config, archive, destination)
    restored = config.model_copy(update={"data_dir": destination})
    await validate_storage(restored)
    identity_db = Database(destination / "identity.db", "identity")
    internal = Database(destination / "internal.db", "internal")
    try:
        service = FeedbackService(
            IdentityService(identity_db),
            internal,
            ScopeCipher(Keyring.load(config.secrets_dir / "keyring.json"), Scope.INTERNAL),
        )
        assert (await ConversationService(service).thread(222, cid))["body"] == BODY
        assert await count(service, "interaction_sessions") == 0
        bot = AsyncMock()
        assert not await DeliveryWorker(service, bot).run_once()
        bot.send_message.assert_not_called()
        before = await count(service, "weekly_reports")
        await StatisticsService(service).tick()
        assert await count(service, "weekly_reports") == before
        assert (destination / ".recovery.json").is_file()
    finally:
        await internal.close()
        await identity_db.close()
    assert await count(s, "messages") == 1


async def test_refuse_overwrite_and_active_runtime(settings, feedback_service, tmp_path):
    config = configured(settings, tmp_path)
    archive = tmp_path / "backups" / "first.afb"
    with RuntimeLock(config.data_dir):
        with pytest.raises(RuntimeBusy):
            await create_backup(config, archive)
        with pytest.raises(RuntimeBusy):
            with RuntimeLock(config.data_dir):
                pass
    await create_backup(config, archive)
    original = archive.read_bytes()
    with pytest.raises(BackupError):
        await create_backup(config, archive)
    assert archive.read_bytes() == original
    with pytest.raises(BackupError):
        await restore_backup(config, archive, config.data_dir)


@pytest.mark.parametrize("mode", ["tag", "truncate", "wrong_key"])
async def test_bad_archive_never_publishes_target(settings, feedback_service, tmp_path, mode):
    config = configured(settings, tmp_path)
    archive = tmp_path / "backups" / "first.afb"
    await create_backup(config, archive)
    if mode == "tag":
        data = bytearray(archive.read_bytes())
        data[-1] ^= 1
        archive.write_bytes(data)
    elif mode == "truncate":
        archive.write_bytes(archive.read_bytes()[:25])
    else:
        create_keyring(tmp_path / "wrong-keys" / "keyring.json")
        config = config.model_copy(update={"secrets_dir": tmp_path / "wrong-keys"})
    target = tmp_path / "restore-target"
    with pytest.raises((BackupError, ValueError, InvalidTag, SecurityError)):
        await restore_backup(config, archive, target)
    assert not target.exists()
    assert not list(tmp_path.glob(".restore-*"))


async def test_archive_path_traversal_is_rejected(settings, feedback_service, tmp_path):
    config = configured(settings, tmp_path)
    source = tmp_path / "malicious.zip"
    with zipfile.ZipFile(source, "w") as bundle:
        bundle.writestr("../escape", "secret")
    archive = tmp_path / "malicious.afb"
    encrypt_archive(source, archive, Keyring.load(config.secrets_dir / "keyring.json"))
    with pytest.raises(BackupError):
        await restore_backup(config, archive, tmp_path / "restored")
    assert not (tmp_path / "escape").exists()


async def test_corrupt_ciphertext_rejected_before_backup_publication(
    settings, feedback_service, tmp_path
):
    s = feedback_service
    await internal_conversation(s)
    async with s.database.transaction(write=True) as conn:
        await conn.execute(update(s.tables["messages"]).values(body_cipher=b"broken"))
    output = tmp_path / "backups" / "bad.afb"
    with pytest.raises(SecurityError):
        await create_backup(configured(settings, tmp_path), output)
    assert not output.exists()


async def test_partial_restore_marker_blocks_startup(
    settings, feedback_service, tmp_path, monkeypatch
):
    import app.services.backup_service as backup

    config = configured(settings, tmp_path)
    archive = tmp_path / "backups" / "good.afb"
    await create_backup(config, archive)
    original = backup.copy_private

    def fail(src, dst):
        if dst.name == "internal.db":
            raise OSError("Simulated disk full")
        return original(src, dst)

    monkeypatch.setattr(backup, "copy_private", fail)
    target = tmp_path / "partial"
    with pytest.raises(OSError):
        await restore_backup(config, archive, target)
    assert (target / ".restore-incomplete").exists()
    with pytest.raises(StorageError):
        await validate_storage(config.model_copy(update={"data_dir": target}))
    await validate_storage(config)


async def test_threaded_file_work_closes_before_cancel_returns():
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def blocking():
        started.set()
        release.wait(5)
        finished.set()

    task = asyncio.create_task(file_work(blocking))
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


async def age(s, cid, days):
    async with s.database.transaction(write=True) as conn:
        c = s.tables["conversations"]
        await conn.execute(
            update(c)
            .where(c.c.id == cid)
            .values(
                created_at=int(s.clock()) - days * 86400,
                updated_at=int(s.clock()) - days * 86400,
                expires_at=int(s.clock()) - 1,
            )
        )


async def test_maintenance_preview_then_purge_foreign_key_order(feedback_service):
    s = feedback_service
    cid = await internal_conversation(s)
    await age(s, cid, 181)
    service = MaintenanceService(s)
    preview = await service.run()
    assert preview["conversations_to_purge"] == 1
    assert await count(s, "messages") == 1
    await service.run(apply=True)
    for name in ("messages", "conversations", "participant_access", "outbox"):
        assert await count(s, name) == 0
    assert (await service.run())["conversations_to_purge"] == 0


async def test_open_report_holds_content_and_snapshot_remains(feedback_service):
    s = feedback_service
    cid, rid, _ = await reported(s)
    await StatisticsService(s).tick()
    await age(s, cid, 181)
    await MaintenanceService(s).run(apply=True)
    assert await count(s, "messages") == 1
    assert not (await ConversationService(s).thread(222, cid))["can_reply"]
    await action(s, 111, "mod_resolve_save", {"id": rid, "accept": False})
    await MaintenanceService(s).run(apply=True)
    assert await count(s, "messages") == 0
    assert await count(s, "reports") == 0
    assert await count(s, "weekly_reports") == 1


async def test_expiry_preserves_activity_and_reclaims_lost_lease(feedback_service):
    s = feedback_service
    cid = await internal_conversation(s)
    await age(s, cid, 91)
    worker = DeliveryWorker(s, AsyncMock())
    await worker.claim()
    now = s.clock()
    s.clock = lambda: now + 121
    result = await MaintenanceService(s).run(apply=True)
    assert result["ambiguous_deliveries"] == 1
    assert await count(s, "messages") == 1
    async with s.database.transaction() as conn:
        row = (await conn.execute(select(s.tables["conversations"]))).mappings().one()
        assert row["status"] == "expired"
        assert row["updated_at"] <= int(now) - 91 * 86400
        assert await conn.scalar(select(s.tables["outbox"].c.status)) == "uncertain"


async def test_maintenance_rollback(feedback_service, monkeypatch):
    s = feedback_service
    cid = await internal_conversation(s)
    await age(s, cid, 181)

    async def fail(*args):
        raise RuntimeError("Simulated failure before commit")

    monkeypatch.setattr(StatisticsService, "reconcile", fail)
    with pytest.raises(RuntimeError):
        await MaintenanceService(s).run(apply=True)
    assert await count(s, "messages") == 1
    assert await count(s, "participant_access") == 2


def test_runtime_lock_released_by_process_exit(tmp_path):
    import subprocess
    import sys

    directory = tmp_path / "locked"
    script = (
        "from pathlib import Path; from app.core.runtime_lock import RuntimeLock; "
        "import os,sys; guard=RuntimeLock(Path(sys.argv[1])); "
        "guard.__enter__(); os._exit(0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(directory)], timeout=30, capture_output=True
    )
    assert result.returncode == 0
    with RuntimeLock(directory):
        pass


async def test_retention_batch_limit(feedback_service, monkeypatch):
    s = feedback_service
    for _ in range(2):
        await age(s, await internal_conversation(s), 181)
    monkeypatch.setattr("app.services.maintenance_service.BATCH_SIZE", 1)
    result = await MaintenanceService(s).run(apply=True)
    assert result["conversations_to_purge"] == 1
    assert await count(s, "conversations") == 1
    await MaintenanceService(s).run(apply=True)
    assert await count(s, "conversations") == 0


async def test_operational_cli_backup_verify_restore_preview_apply(
    settings, feedback_service, tmp_path
):
    import os
    import subprocess
    import sys

    from tests.conftest import TEST_TOKEN

    s = feedback_service
    await age(s, await internal_conversation(s), 181)
    config = configured(settings, tmp_path)
    environment = {
        **os.environ,
        "BOT_TOKEN": TEST_TOKEN,
        "BOOTSTRAP_ADMIN_IDS": "[111]",
        "DATA_DIR": str(config.data_dir),
        "SECRETS_DIR": str(config.secrets_dir),
    }

    def run(*args):
        result = subprocess.run(
            [sys.executable, "-m", "app.cli", *args],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout.decode("utf-8")
        assert TEST_TOKEN.encode() not in result.stdout + result.stderr
        assert b"Traceback" not in result.stdout + result.stderr
        return result.stdout.decode("utf-8")

    archive = tmp_path / "backups" / "cli.afb"
    await asyncio.to_thread(run, "backup", "--output", str(archive))
    await asyncio.to_thread(run, "backup-check", "--source", str(archive))
    await asyncio.to_thread(
        run, "restore", "--source", str(archive), "--target", str(tmp_path / "recovered")
    )
    assert "فقط پیش‌نمایش" in await asyncio.to_thread(run, "maintenance")
    assert await count(s, "messages") == 1
    await asyncio.to_thread(run, "maintenance", "--apply")
    assert await count(s, "messages") == 0
