import asyncio
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from app import main as application
from app.core.runtime_lock import RuntimeLock
from app.core.security import SecurityError, create_keyring
from app.database.storage import StorageError, validate_storage


@pytest.mark.parametrize("retention", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
async def test_lifecycle_shutdown_and_restart(
    settings, identity_service, tmp_path, monkeypatch, retention, cancel
):
    config = settings.model_copy(
        update={
            "data_dir": tmp_path / "data",
            "secrets_dir": tmp_path / "keys",
            "retention_enabled": retention,
        }
    )
    started, stopped = set(), set()
    expected = {"delivery", "statistics"} | ({"maintenance"} if retention else set())
    ready = asyncio.Event()

    def worker(name):
        async def run(*_args):
            started.add(name)
            if started == expected:
                ready.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.add(name)

        return run

    monkeypatch.setattr(application, "run_deliveries", worker("delivery"))
    monkeypatch.setattr(application, "run_statistics", worker("statistics"))
    monkeypatch.setattr(application, "run_maintenance", worker("maintenance"))
    bot = AsyncMock()
    monkeypatch.setattr(application, "Bot", Mock(return_value=bot))

    async def polling(*args, **kwargs):
        await asyncio.wait_for(ready.wait(), timeout=10)
        if cancel:
            raise asyncio.CancelledError

    dispatcher = Mock(start_polling=AsyncMock(side_effect=polling))
    monkeypatch.setattr(application, "create_dispatcher", Mock(return_value=dispatcher))
    for _ in range(2):
        started.clear()
        stopped.clear()
        ready.clear()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await application.run_bot(config)
        else:
            await application.run_bot(config)
        assert stopped == expected
        with RuntimeLock(config.data_dir):
            await validate_storage(config)
    assert bot.get_me.await_count == 2
    assert bot.session.close.await_count == 2


@pytest.mark.parametrize("with_keys", [False, True])
async def test_uninitialized_release_never_contacts_telegram(
    settings, tmp_path, monkeypatch, with_keys
):
    config = settings.model_copy(
        update={"data_dir": tmp_path / "data", "secrets_dir": tmp_path / "keys"}
    )
    bot = Mock()
    monkeypatch.setattr(application, "Bot", bot)
    if with_keys:
        create_keyring(config.secrets_dir / "keyring.json")
    with pytest.raises(StorageError if with_keys else SecurityError):
        await application.run_bot(config)
    bot.assert_not_called()


def test_installed_cli_offline_acceptance():
    script = Path(__file__).resolve().parents[1] / "deploy" / "smoke.py"
    result = subprocess.run(
        [sys.executable, str(script)], capture_output=True, timeout=180, check=False
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert b"checks passed" in result.stdout
