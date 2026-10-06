import os
import subprocess
import sys

from tests.conftest import TEST_TOKEN


def test_cli_setup_idempotency_and_health(tmp_path):
    environment = {
        **os.environ,
        "BOT_TOKEN": TEST_TOKEN,
        "BOOTSTRAP_ADMIN_IDS": "[111]",
        "DATA_DIR": str(tmp_path / "data"),
        "SECRETS_DIR": str(tmp_path / "secrets"),
    }

    def run(command):
        result = subprocess.run(
            [sys.executable, "-m", "app.cli", command],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            timeout=40,
            check=False,
        )
        assert TEST_TOKEN.encode() not in result.stdout + result.stderr
        assert b"Traceback" not in result.stderr
        return result

    assert run("db-check").returncode == 2
    assert run("keys-init").returncode == 0
    original = (tmp_path / "secrets" / "keyring.json").read_bytes()
    assert run("keys-init").returncode == 2
    assert (tmp_path / "secrets" / "keyring.json").read_bytes() == original
    assert run("db-upgrade").returncode == 0
    assert run("db-upgrade").returncode == 0
    assert b"external: OK" in run("db-check").stdout
    assert run("admin-bootstrap").returncode == 0
    repeated = run("admin-bootstrap")
    assert repeated.returncode == 0
    assert "قبلاً" in repeated.stdout.decode("utf-8")
    (tmp_path / "secrets" / "keyring.json").unlink()
    assert run("keys-init").returncode == 2
    assert not (tmp_path / "secrets" / "keyring.json").exists()
