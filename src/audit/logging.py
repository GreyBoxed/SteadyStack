"""JSON log formatting.

Logs are read twice: once at a terminal while something is being built, and
once in CloudWatch years later while something is being explained. The second
reader needs to filter on a trace ID, so the format is JSON everywhere rather
than pretty locally and structured in production -- two formats mean the one
that matters is the one nobody tested.

Extras passed to a logging call are merged into the object, so a message never
has to be parsed to recover a value that was structured when it was written.

Use `fields()` to pass them. Python's logging refuses an `extra` key that
collides with a LogRecord attribute -- `created`, `name`, `module`, `args`
and a dozen others -- by raising, inside the logging call, in the code being
logged. Those are ordinary words in this domain (`created` instruments,
`name` of an instrument), so every call site would be one rename away from
crashing. `fields()` carries them in a single attribute that cannot collide.
"""

import json
import logging
from typing import Any

from audit.context import current_request_id, current_trace_id

#: The single attribute structured values travel in, so that a domain word
#: like "created" never has to fight LogRecord for its name.
_FIELDS_KEY = "steadystack_fields"

#: Attributes LogRecord always carries. Anything outside this set arrived via
#: `extra=` and belongs in the emitted object.
_STANDARD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


def fields(**values: Any) -> dict[str, Any]:
    """Wrap structured values for `extra=`, collision-free.

    logger.info("refreshed", extra=fields(created=12, name="INFY"))
    """
    return {_FIELDS_KEY: values}


class JSONFormatter(logging.Formatter):
    """One JSON object per line, with the trace and request IDs always present."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": current_trace_id(),
        }

        request_id = current_request_id()
        if request_id:
            payload["request_id"] = request_id

        carried = record.__dict__.get(_FIELDS_KEY)
        if isinstance(carried, dict):
            payload.update(carried)

        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and key != _FIELDS_KEY and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        # A value that cannot be serialised must not lose the whole line --
        # a log call is not worth an exception in the code being logged.
        return json.dumps(payload, default=str)
