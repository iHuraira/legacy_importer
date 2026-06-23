"""Logging setup that avoids exposing connection strings or credentials."""

from __future__ import annotations

import logging
import re
from typing import Any


class SecretFilter(logging.Filter):
    """Redact passwords embedded in PostgreSQL-style URLs."""

    _url_password = re.compile(r"(://[^:/\s]+:)([^@\s]+)(@)")

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        record.msg = self._url_password.sub(r"\1***\3", message)
        record.args = ()
        return True


def configure_logging(verbose: bool = False) -> None:
    """Configure concise console logging for CLI commands."""

    handler = logging.StreamHandler()
    handler.addFilter(SecretFilter())
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[handler],
        force=True,
    )


def safe_error(exc: Exception) -> str:
    """Return a redacted exception message suitable for a failure report."""

    record = logging.LogRecord("", 0, "", 0, str(exc), (), None)
    SecretFilter().filter(record)
    return str(record.msg)
