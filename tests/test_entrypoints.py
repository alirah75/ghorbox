import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("arguments", [["app.cli", "check-config"], ["app.main"]])
def test_windows_redirected_output_handles_persian(tmp_path, arguments):
    environment = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    result = subprocess.run(
        [sys.executable, "-m", *arguments],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2
    assert "تنظیمات معتبر نیست" in result.stdout.decode("utf-8")
    assert b"Traceback" not in result.stderr
