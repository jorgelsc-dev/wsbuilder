import re


_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


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
    header_value = validate_header_value(value)
    target = normalize_header_name(header_name)
    for key in list(headers.keys()):
        if normalize_header_name(key) == target:
            if overwrite:
                headers[key] = header_value
            return
    headers[header_name] = header_value
