"""Validation for SQLite ``PRAGMA name=value`` statements.

PRAGMA values cannot be bound as parameters, so they are checked before being
interpolated: numbers, booleans, and bare keywords/identifiers only.
"""

import math
import re

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_KEYWORD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NUMBER_RE = re.compile(r"^[+-]?[0-9]+(\.[0-9]+)?$")


def pragma_value_sql(value):
    """Return ``value`` as safe PRAGMA SQL text, or raise ``ValueError``."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"Unsafe PRAGMA value: {value!r}")
        return repr(value)
    if isinstance(value, str):
        text = value.strip()
        if _KEYWORD_RE.match(text) or _NUMBER_RE.match(text):
            return text
    raise ValueError(f"Unsafe PRAGMA value: {value!r}")


def pragma_assignment(name, value):
    """``PRAGMA name=value`` with both parts validated."""
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return f"PRAGMA {name}={pragma_value_sql(value)}"
