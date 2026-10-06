"""UTF-8 diagnostics, including Windows redirected output."""

import io
import sys


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
