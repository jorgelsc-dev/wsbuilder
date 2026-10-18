import re

_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


_LOWER_TOKEN_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9a-z]+$")


def field_name_problem(name):
    """Why ``name`` is not a valid HTTP/2 or HTTP/3 field name, or ``None``."""
    if not _LOWER_TOKEN_RE.fullmatch(name or ""):
        return f"invalid header name {name!r}"
    return None


def field_value_problem(value):
    """Why ``value`` is not a valid HTTP/2 or HTTP/3 field value, or ``None``.

    RFC 9113 section 8.2.1 and RFC 9114 section 4.2: no CR, LF or NUL, and no
    leading or trailing space or tab.
    """
    text = str(value)
    if any(char in text for char in ("\r", "\n", "\x00")):
        return "header value contains CR, LF or NUL"
    if text[:1] in (" ", "\t") or text[-1:] in (" ", "\t"):
        return "header value has leading or trailing whitespace"
    return None


def combine_field_values(fields):
    """Fold repeated fields into one value per name, as HTTP/1 parsing does.

    ``cookie`` crumbs are joined with ``"; "`` (RFC 9113 section 8.2.3); any
    other repeated field with ``", "``.
    """
    headers = {}
    for name, value in fields:
        if name in headers:
            separator = "; " if name == "cookie" else ", "
            headers[name] = f"{headers[name]}{separator}{value}"
        else:
            headers[name] = value
    return headers


def normalize_header_name(name):
    return str(name or "").strip().lower()


def validate_header_name(name):
    text = str(name or "")
    if not _HEADER_NAME_RE.fullmatch(text):
        raise ValueError(f"Invalid HTTP header name: {text!r}")
    return text


def validate_header_value(value):
    text = str(value)
    if any(char in text for char in ("\r", "\n", "\x00")):
        raise ValueError("HTTP header values must not contain CR, LF or NUL")
    if any(ord(char) < 32 and char != "\t" for char in text):
        raise ValueError("HTTP header values must not contain control characters")
    return text


def validate_header_field_value(value):
    """Validate one value, or a list/tuple of values sent as repeated fields.

    A list keeps each value as its own field line (needed for ``Set-Cookie``,
    which cannot be comma-joined); a single value stays a string.
    """
    if isinstance(value, (list, tuple)):
        if not value:
            raise ValueError("HTTP header value lists must not be empty")
        return [validate_header_value(item) for item in value]
    return validate_header_value(value)


def header_values(value):
    """The field values ``value`` stands for, as a list of strings."""
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return [str(value)]


def header_value_text(value):
    """``value`` as one string; list values are joined with ``", "``."""
    return ", ".join(header_values(value))


def iter_header_fields(headers):
    """Yield ``(name, value)`` per field line, expanding list values."""
    for name, value in (headers or {}).items():
        for item in header_values(value):
            yield name, item


def add_header(headers, name, value):
    """Append ``value`` to ``name``, keeping any existing values as a list."""
    if headers is None:
        raise ValueError("headers container is required")
    header_name = validate_header_name(name)
    new_values = header_values(validate_header_field_value(value))
    target = normalize_header_name(header_name)
    for key in list(headers.keys()):
        if normalize_header_name(key) == target:
            headers[key] = header_values(headers[key]) + new_values
            return
    headers[header_name] = new_values[0] if len(new_values) == 1 else new_values


def get_header(headers, name, default=""):
    if not headers:
        return default
    target = normalize_header_name(name)
    for key, value in headers.items():
        if normalize_header_name(key) == target:
            return value
    return default


def has_header(headers, name):
    marker = object()
    return get_header(headers, name, default=marker) is not marker


def set_header(headers, name, value, overwrite=True):
    if headers is None:
        raise ValueError("headers container is required")
    header_name = validate_header_name(name)
    header_value = validate_header_field_value(value)
    target = normalize_header_name(header_name)
    for key in list(headers.keys()):
        if normalize_header_name(key) == target:
            if overwrite:
                headers[key] = header_value
            return
    headers[header_name] = header_value
