"""Logging for permit_mcp: loggers that never write a registered secret.

This is the only module that calls `logging.getLogger`. Every logger it returns, and the
"permit_mcp" logger above them, carries one filter that replaces each registered secret
with "[REDACTED]" in the message, its arguments, the traceback and the stack of a record.
"""

import collections
import logging
import sys
import threading

ROOT_LOGGER = "permit_mcp"
REDACTED = "[REDACTED]"
# Short-lived credentials kept registered after their call ends, so an error or log record
# that echoes one later is still redacted, while the list of secrets stays bounded.
RECENT_SECRETS = 1024


class _Redactor(logging.Filter):
    """Replaces every registered secret with `REDACTED` in log records and text."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._counts: collections.Counter[str] = collections.Counter()
        # Longest first, so a secret that contains another is replaced whole. Replaced, never
        # mutated, so a thread that scrubs while another registers reads a consistent tuple.
        self._secrets: tuple[str, ...] = ()
        self._recent: collections.deque[str] = collections.deque()

    def add(self, secret: str) -> None:
        with self._lock:
            for form in _forms(secret):
                self._counts[form] += 1
            self._rebuild()

    def add_recent(self, secret: str) -> None:
        with self._lock:
            for form in _forms(secret):
                self._counts[form] += 1
            self._recent.append(secret)
            if len(self._recent) > RECENT_SECRETS:
                for form in _forms(self._recent.popleft()):
                    self._counts[form] -= 1
                    if self._counts[form] <= 0:
                        del self._counts[form]
            self._rebuild()

    def _rebuild(self) -> None:
        self._secrets = tuple(sorted(self._counts, key=len, reverse=True))

    def scrub(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            # A malformed format string: logging reports it later with msg and args, so
            # those must not hold a secret either.
            message = f"{record.msg} {record.args}"
        record.msg = self.scrub(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.scrub(record.exc_text)
        if record.stack_info:
            record.stack_info = self.scrub(record.stack_info)
        return True


def _forms(secret: str) -> set[str]:
    """Return the secret as given and without surrounding whitespace, minus empty forms."""
    return {form for form in (secret, secret.strip()) if form.strip()}


_redactor = _Redactor()
logging.getLogger(ROOT_LOGGER).addFilter(_redactor)


def get_logger(name: str) -> logging.Logger:
    """Return the logger `name` under "permit_mcp", with the redacting filter attached.

    A logger's filters see only the records logged on that logger, not those its children
    pass up, so each logger handed out gets the filter itself.

    Args:
        name: A module name such as `__name__`; a name outside "permit_mcp" is put under it.

    Returns:
        The logger.

    """
    if name != ROOT_LOGGER and not name.startswith(f"{ROOT_LOGGER}."):
        name = f"{ROOT_LOGGER}.{name}"
    logger = logging.getLogger(name)
    if _redactor not in logger.filters:
        logger.addFilter(_redactor)
    return logger


def redact(secret: str) -> None:
    """Replace `secret` with "[REDACTED]" in every record and error text from now on.

    The secret without surrounding whitespace is replaced too. A secret that is empty or
    only whitespace is ignored.

    Args:
        secret: A credential, such as an API key.

    """
    _redactor.add(secret)


def redact_recent(secret: str) -> None:
    """Replace `secret` with "[REDACTED]" for as long as it is among the recent secrets.

    For short-lived credentials, such as a per-call Elements token: the last
    `RECENT_SECRETS` stay registered, so one echoed after its call is still redacted, and
    registering one per call does not grow the list without bound.

    Args:
        secret: The credential.

    """
    _redactor.add_recent(secret)


def scrub(text: str) -> str:
    """Return `text` with every registered secret replaced with "[REDACTED]".

    Args:
        text: Text bound for a log record or an error message.

    Returns:
        The text without any registered secret.

    """
    return _redactor.scrub(text)


def configure_cli_logging() -> None:
    """Send every log record of the process to stderr, with secrets redacted.

    For the `permit-mcp` command: stdout carries the stdio protocol, so nothing else may
    write to it. The handler goes on the root logger, so the records of the MCP SDK and of
    aiohttp go to stderr too, and it carries the redacting filter for them.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    handler.addFilter(_redactor)
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
