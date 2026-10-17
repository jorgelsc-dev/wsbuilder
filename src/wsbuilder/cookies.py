import re

from .headers import get_header


_COOKIE_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_COOKIE_VALUE_RE = re.compile(r'^[\x21\x23-\x2B\x2D-\x3A\x3C-\x5B\x5D-\x7E]*$')
_SAME_SITE_VALUES = {
    "lax": "Lax",
    "strict": "Strict",
    "none": "None",
}


def _validate_cookie_attribute(name, value):
    text = str(value)
    if any(char in text for char in ("\r", "\n", "\x00", ";")):
        raise ValueError(f"{name} contains invalid cookie attribute characters")
    return text


def parse_cookie_header(cookie_header):
    parsed = {}
    raw = str(cookie_header or "").strip()
    if not raw:
        return parsed
    for chunk in raw.split(";"):
        part = chunk.strip()
        if not part:
            continue
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        if not key:
            continue
        parsed[key] = value.strip()
    return parsed


def get_cookie(headers, name, default=""):
    cookie_text = get_header(headers, "cookie", default="")
    cookies = parse_cookie_header(cookie_text)
    return cookies.get(name, default)


def build_set_cookie(
    name,
    value,
    path="/",
    max_age=None,
    domain=None,
    secure=False,
    http_only=False,
    same_site="Lax",
):
    cookie_name = str(name or "")
    cookie_value = str(value)
    if not _COOKIE_NAME_RE.fullmatch(cookie_name):
        raise ValueError(f"Invalid cookie name: {cookie_name!r}")
    if not _COOKIE_VALUE_RE.fullmatch(cookie_value):
        raise ValueError("Cookie value contains invalid characters")

    chunks = [f"{cookie_name}={cookie_value}"]
    if path:
        chunks.append(f"Path={_validate_cookie_attribute('Path', path)}")
    if domain:
        chunks.append(f"Domain={_validate_cookie_attribute('Domain', domain)}")
    if max_age is not None:
        chunks.append(f"Max-Age={int(max_age)}")
    if secure:
        chunks.append("Secure")
    if http_only:
        chunks.append("HttpOnly")
    if same_site:
        normalized = _SAME_SITE_VALUES.get(str(same_site).strip().lower())
        if normalized is None:
            raise ValueError("same_site must be one of: Lax, Strict, None")
        chunks.append(f"SameSite={normalized}")
    return "; ".join(chunks)
