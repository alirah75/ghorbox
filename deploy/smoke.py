"""Offline installed-package acceptance check; only creates temporary fake data."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    with tempfile.TemporaryDirectory(prefix="feedback-smoke-") as directory:
        root = Path(directory)
        environment = {
            **os.environ,
            "BOT_TOKEN": "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
            "BOOTSTRAP_ADMIN_IDS": "[111]",
            "APP_TIMEZONE": "Asia/Tehran",
            "DATA_DIR": str(root / "data"),
            "SECRETS_DIR": str(root / "keys"),
            "RETENTION_ENABLED": "false",
            "PYTHONIOENCODING": "utf-8",
        }
        archive = str(root / "backups" / "smoke.afb")
        commands = [
            ["check-config"],
            ["keys-init"],
            ["db-upgrade"],
            ["db-check"],
            ["admin-bootstrap"],
            ["backup", "--output", archive],
            ["backup-check", "--source", archive],
            ["restore", "--source", archive, "--target", str(root / "restored")],
            ["maintenance"],
        ]
        for arguments in commands:
            result = subprocess.run(
                [sys.executable, "-m", "app.cli", *arguments],
                cwd=root,
                env=environment,
                capture_output=True,
                timeout=90,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Offline check failed: {arguments[0]}")
        assert (root / "restored" / ".recovery.json").is_file()
        assert not (root / "restored" / ".restore-incomplete").exists()
    print("Offline installed-package checks passed; no Telegram request was sent.")


if __name__ == "__main__":
    main()
