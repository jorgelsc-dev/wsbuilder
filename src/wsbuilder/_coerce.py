"""Lenient number coercion shared by config-reading modules."""


def safe_int(value, default):
    """``int(value)``, or ``default`` when the value cannot be converted."""
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def safe_float(value, default):
    """``float(value)``, or ``default`` when the value cannot be converted."""
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default
