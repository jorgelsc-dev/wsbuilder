from .headers import get_header


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
    chunks = [f"{name}={value}"]
    if path:
        chunks.append(f"Path={path}")
    if domain:
        chunks.append(f"Domain={domain}")
    if max_age is not None:
        chunks.append(f"Max-Age={int(max_age)}")
    if secure:
        chunks.append("Secure")
    if http_only:
        chunks.append("HttpOnly")
    if same_site:
        chunks.append(f"SameSite={same_site}")
    return "; ".join(chunks)

