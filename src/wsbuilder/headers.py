def normalize_header_name(name):
    return str(name or "").strip().lower()


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
    target = normalize_header_name(name)
    for key in list(headers.keys()):
        if normalize_header_name(key) == target:
            if overwrite:
                headers[key] = value
            return
    headers[name] = value

