"""Database error values out of the server's logs (TABRION MIG-NOD-3, finding B-06-2).

A PostgreSQL error carries the server's ``DETAIL`` (for a constraint violation the offending values, ``Key
(email)=(someone@example.com) already exists.``), and so does every traceback that logging prints for it, also when the
error is only the cause (``raise ... from exc``) of a Nodestro error. Tabrion's
:class:`~tabrion.errors.DbErrorRedactionFilter` rewrites a log record so that its message, arguments and traceback
describe each database error by kind, SQLSTATE and object names only; Tabrion installs it nowhere.

:func:`install_db_error_redaction` applies it to every log record of a server process (``nodestro-server`` and the
app of :func:`nodestro_api.server.create_server_app`). It wraps the log record factory, so the filter runs when a
record is created: before any logger filter, handler or formatter sees it, for uvicorn's loggers as for Nodestro's,
Axentra's and Orkexa's, and also after a later :func:`logging.config.dictConfig` (which leaves the factory alone).
It also adds the filter to ``logging.lastResort`` and to the handlers it is given. Installing twice is harmless.

``tabrion`` is a base dependency of nodestro-runtime (doc 13: nodestro-api -> tabrion is transitive); only the
server profile imports this module, and only its standard-library core (:mod:`tabrion.errors`).
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from tabrion.errors import DbErrorRedactionFilter

__all__ = ["DbErrorRedactionFilter", "install_db_error_redaction"]

#: Marks the record factory this module installed (the attribute is set on the wrapping function).
_MARK = "_nodestro_db_error_redaction"
_LOCK = threading.Lock()


def _add_filter(target: logging.Filterer) -> None:
    if not any(isinstance(item, DbErrorRedactionFilter) for item in target.filters):
        target.addFilter(DbErrorRedactionFilter())


def install_db_error_redaction(*handlers: logging.Handler) -> None:
    """Run Tabrion's :class:`DbErrorRedactionFilter` on every log record of the process (see the module docs), and
    add it to ``logging.lastResort`` and to ``handlers``."""
    with _LOCK:
        current = logging.getLogRecordFactory()
        if not getattr(current, _MARK, False):
            redaction = DbErrorRedactionFilter()

            def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
                record = current(*args, **kwargs)
                redaction.filter(record)  # never drops a record; rewrites database error text in it
                return record

            setattr(factory, _MARK, True)
            logging.setLogRecordFactory(factory)
        targets: list[logging.Filterer] = list(handlers)
        if logging.lastResort is not None:
            targets.append(logging.lastResort)
        for target in targets:
            _add_filter(target)
