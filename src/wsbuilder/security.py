"""Security policy engine with ACL, allow/deny lists and behavior-based blocking."""

from __future__ import annotations

import ipaddress
import re
import threading
import time
from collections import deque
from dataclasses import dataclass

from .http import Response

DEFAULT_RATE_LIMIT_REQUESTS = 240
DEFAULT_RATE_LIMIT_WINDOW_SECONDS = 60.0
DEFAULT_VIOLATION_THRESHOLD = 10
DEFAULT_VIOLATION_WINDOW_SECONDS = 60.0
DEFAULT_SUSPICIOUS_THRESHOLD = 24
DEFAULT_SUSPICIOUS_WINDOW_SECONDS = 120.0
DEFAULT_BLOCK_DURATION_SECONDS = 300.0


def _safe_int(value, default):
    try:
        return int(value)
    except Exception:
        return default


def _safe_float(value, default):
    try:
        return float(value)
    except Exception:
        return default


def _to_ip(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return ipaddress.ip_address(text)
    except Exception:
        return None


def _to_network(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return ipaddress.ip_network(text, strict=False)
    except Exception:
        ip_obj = _to_ip(text)
        if ip_obj is None:
            return None
        suffix = "32" if ip_obj.version == 4 else "128"
        return ipaddress.ip_network(f"{ip_obj}/{suffix}", strict=False)


def _ip_in_networks(ip_text, networks):
    ip_obj = _to_ip(ip_text)
    if ip_obj is None:
        return False
    for net in networks:
        try:
            if ip_obj in net:
                return True
        except Exception:
            continue
    return False


@dataclass(slots=True)
class SecurityDecision:
    allowed: bool
    status: int = 200
    reason: str = "allowed"
    message: str = "Allowed"
    rule_name: str = ""
    client_ip: str = ""
    retry_after: int = 0

    def response_headers(self):
        headers = {"X-WSBuilder-Security-Reason": self.reason}
        if self.retry_after > 0 and int(self.status) == 429:
            headers["Retry-After"] = str(int(self.retry_after))
        return headers

    def to_response(self):
        return Response.text(self.message, status=int(self.status), headers=self.response_headers())


class ACLRule:
    def __init__(
        self,
        *,
        name="",
        effect="deny",
        methods=None,
        path=None,
        path_prefix=None,
        path_regex=None,
        ip_cidrs=None,
        require_tls=None,
        header_equals=None,
        header_regex=None,
    ):
        effect_text = str(effect or "").strip().lower()
        if effect_text not in {"allow", "deny"}:
            raise ValueError("ACL effect must be 'allow' or 'deny'")

        self.name = str(name or "").strip()
        self.effect = effect_text
        self.methods = {str(m).strip().upper() for m in (methods or ()) if str(m).strip()}
        self.path = str(path or "").strip() or ""
        self.path_prefix = str(path_prefix or "").strip() or ""
        self.path_regex = str(path_regex or "").strip() or ""
        self._path_regex_compiled = re.compile(self.path_regex) if self.path_regex else None
        self.require_tls = None if require_tls is None else bool(require_tls)

        self.networks = []
        for item in ip_cidrs or ():
            net = _to_network(item)
            if net is not None:
                self.networks.append(net)

        self.header_equals = {}
        for key, value in (header_equals or {}).items():
            self.header_equals[str(key or "").strip().lower()] = str(value or "")

        self.header_regex = {}
        for key, pattern in (header_regex or {}).items():
            header_key = str(key or "").strip().lower()
            pattern_text = str(pattern or "").strip()
            if not pattern_text:
                continue
            self.header_regex[header_key] = re.compile(pattern_text)

    def matches(self, request, client_ip):
        if self.methods and request.method not in self.methods:
            return False
        if self.path and request.path != self.path:
            return False
        if self.path_prefix and not request.path.startswith(self.path_prefix):
            return False
        if self._path_regex_compiled and not self._path_regex_compiled.search(request.path):
            return False
        if self.require_tls is not None:
            tls_enabled = bool((request.tls or {}).get("enabled"))
            if tls_enabled != self.require_tls:
                return False
        if self.networks and not _ip_in_networks(client_ip, self.networks):
            return False
        for key, expected in self.header_equals.items():
            if str(request.headers.get(key, "")) != expected:
                return False
        for key, regex in self.header_regex.items():
            if not regex.search(str(request.headers.get(key, ""))):
                return False
        return True

    def describe(self):
        return {
            "name": self.name,
            "effect": self.effect,
            "methods": sorted(self.methods),
            "path": self.path,
            "path_prefix": self.path_prefix,
            "path_regex": self.path_regex,
            "ip_cidrs": [str(n) for n in self.networks],
            "require_tls": self.require_tls,
            "header_equals": dict(self.header_equals),
            "header_regex": {k: r.pattern for k, r in self.header_regex.items()},
        }


class SecurityPolicy:
    def __init__(
        self,
        *,
        acl_default="allow",
        trust_x_forwarded_for=False,
        whitelist_overrides_blacklist=True,
        whitelist_bypass_behavior=True,
        rate_limit_requests=DEFAULT_RATE_LIMIT_REQUESTS,
        rate_limit_window_seconds=DEFAULT_RATE_LIMIT_WINDOW_SECONDS,
        violation_threshold=DEFAULT_VIOLATION_THRESHOLD,
        violation_window_seconds=DEFAULT_VIOLATION_WINDOW_SECONDS,
        suspicious_status_codes=(401, 403, 404, 429),
        suspicious_threshold=DEFAULT_SUSPICIOUS_THRESHOLD,
        suspicious_window_seconds=DEFAULT_SUSPICIOUS_WINDOW_SECONDS,
        block_duration_seconds=DEFAULT_BLOCK_DURATION_SECONDS,
    ):
        acl_default_text = str(acl_default or "").strip().lower()
        if acl_default_text not in {"allow", "deny"}:
            raise ValueError("acl_default must be 'allow' or 'deny'")

        self.acl_default = acl_default_text
        self.trust_x_forwarded_for = bool(trust_x_forwarded_for)
        self.whitelist_overrides_blacklist = bool(whitelist_overrides_blacklist)
        self.whitelist_bypass_behavior = bool(whitelist_bypass_behavior)

        self.rate_limit_requests = max(0, _safe_int(rate_limit_requests, DEFAULT_RATE_LIMIT_REQUESTS))
        self.rate_limit_window_seconds = max(
            0.0,
            _safe_float(rate_limit_window_seconds, DEFAULT_RATE_LIMIT_WINDOW_SECONDS),
        )
        self.violation_threshold = max(0, _safe_int(violation_threshold, DEFAULT_VIOLATION_THRESHOLD))
        self.violation_window_seconds = max(
            0.0,
            _safe_float(violation_window_seconds, DEFAULT_VIOLATION_WINDOW_SECONDS),
        )
        self.suspicious_status_codes = {
            int(code)
            for code in (suspicious_status_codes or ())
            if _safe_int(code, None) is not None
        }
        self.suspicious_threshold = max(0, _safe_int(suspicious_threshold, DEFAULT_SUSPICIOUS_THRESHOLD))
        self.suspicious_window_seconds = max(
            0.0,
            _safe_float(suspicious_window_seconds, DEFAULT_SUSPICIOUS_WINDOW_SECONDS),
        )
        self.block_duration_seconds = max(1.0, _safe_float(block_duration_seconds, DEFAULT_BLOCK_DURATION_SECONDS))

        self._lock = threading.Lock()
        self._acl_rules = []
        self._whitelist_networks = []
        self._blacklist_networks = []
        self._temporary_blocks = {}
        self._request_events = {}
        self._violation_events = {}
        self._suspicious_events = {}

        self._requests_total = 0
        self._allowed_total = 0
        self._blocked_total = 0
        self._temporary_blocks_total = 0
        self._acl_allow_total = 0
        self._acl_deny_total = 0
        self._blocked_by_reason = {}

    def _inc_map(self, data, key, step=1):
        data[key] = data.get(key, 0) + int(step)

    def _expire_blocks_locked(self, now):
        expired = [ip for ip, row in self._temporary_blocks.items() if float(row.get("until", 0.0)) <= now]
        for ip in expired:
            self._temporary_blocks.pop(ip, None)

    def _prune_events_locked(self, store, now, window_seconds):
        if window_seconds <= 0:
            store.clear()
            return
        threshold = now - window_seconds
        for key, rows in list(store.items()):
            while rows and rows[0] <= threshold:
                rows.popleft()
            if not rows:
                store.pop(key, None)

    def _push_event_locked(self, store, key, now, window_seconds):
        rows = store.get(key)
        if rows is None:
            rows = deque()
            store[key] = rows
        rows.append(now)
        threshold = now - window_seconds
        while rows and rows[0] <= threshold:
            rows.popleft()
        return len(rows)

    def _set_temporary_block_locked(self, ip_text, reason, now, duration_seconds=None):
        duration = self.block_duration_seconds
        if duration_seconds is not None:
            duration = max(1.0, _safe_float(duration_seconds, self.block_duration_seconds))
        until = now + duration
        row = self._temporary_blocks.get(ip_text)
        if row and float(row.get("until", 0.0)) > until:
            until = float(row.get("until"))
        self._temporary_blocks[ip_text] = {"reason": str(reason or "temporary_block"), "until": until}
        self._temporary_blocks_total += 1
        return until

    def _register_violation_locked(self, ip_text, now):
        if self.violation_threshold <= 0 or self.violation_window_seconds <= 0:
            return
        count = self._push_event_locked(
            self._violation_events,
            ip_text,
            now,
            self.violation_window_seconds,
        )
        if count >= self.violation_threshold:
            self._set_temporary_block_locked(ip_text, "violation_threshold", now)
            self._violation_events.pop(ip_text, None)

    def _register_suspicious_response_locked(self, ip_text, now):
        if self.suspicious_threshold <= 0 or self.suspicious_window_seconds <= 0:
            return
        count = self._push_event_locked(
            self._suspicious_events,
            ip_text,
            now,
            self.suspicious_window_seconds,
        )
        if count >= self.suspicious_threshold:
            self._set_temporary_block_locked(ip_text, "suspicious_responses", now)
            self._suspicious_events.pop(ip_text, None)

    def _deny_locked(self, *, ip_text, now, status, reason, message, rule_name="", retry_after=0):
        self._blocked_total += 1
        self._inc_map(self._blocked_by_reason, reason)
        self._register_violation_locked(ip_text, now)
        return SecurityDecision(
            allowed=False,
            status=int(status),
            reason=str(reason),
            message=str(message),
            rule_name=str(rule_name or ""),
            client_ip=ip_text,
            retry_after=max(0, int(retry_after or 0)),
        )

    def _allow_locked(self, ip_text):
        self._allowed_total += 1
        return SecurityDecision(
            allowed=True,
            status=200,
            reason="allowed",
            message="Allowed",
            client_ip=ip_text,
        )

    def resolve_client_ip(self, request):
        if self.trust_x_forwarded_for:
            forwarded = str(request.headers.get("x-forwarded-for", "")).strip()
            if forwarded:
                first = forwarded.split(",")[0].strip()
                if _to_ip(first):
                    return first
        client = request.client or ("", 0)
        ip_text = str(client[0] if isinstance(client, (tuple, list)) and client else "")
        if _to_ip(ip_text):
            return ip_text
        return ""

    def add_whitelist(self, *ip_or_cidr):
        added = 0
        with self._lock:
            existing = {str(n) for n in self._whitelist_networks}
            for raw in ip_or_cidr:
                net = _to_network(raw)
                if net is None:
                    continue
                key = str(net)
                if key in existing:
                    continue
                self._whitelist_networks.append(net)
                existing.add(key)
                added += 1
        return added

    def add_blacklist(self, *ip_or_cidr):
        added = 0
        with self._lock:
            existing = {str(n) for n in self._blacklist_networks}
            for raw in ip_or_cidr:
                net = _to_network(raw)
                if net is None:
                    continue
                key = str(net)
                if key in existing:
                    continue
                self._blacklist_networks.append(net)
                existing.add(key)
                added += 1
        return added

    def clear_whitelist(self):
        with self._lock:
            self._whitelist_networks = []

    def clear_blacklist(self):
        with self._lock:
            self._blacklist_networks = []

    def add_acl_rule(
        self,
        *,
        name="",
        effect="deny",
        methods=None,
        path=None,
        path_prefix=None,
        path_regex=None,
        ip_cidrs=None,
        require_tls=None,
        header_equals=None,
        header_regex=None,
    ):
        rule = ACLRule(
            name=name,
            effect=effect,
            methods=methods,
            path=path,
            path_prefix=path_prefix,
            path_regex=path_regex,
            ip_cidrs=ip_cidrs,
            require_tls=require_tls,
            header_equals=header_equals,
            header_regex=header_regex,
        )
        with self._lock:
            self._acl_rules.append(rule)
        return rule

    def allow(self, **kwargs):
        kwargs["effect"] = "allow"
        return self.add_acl_rule(**kwargs)

    def deny(self, **kwargs):
        kwargs["effect"] = "deny"
        return self.add_acl_rule(**kwargs)

    def clear_acl(self):
        with self._lock:
            self._acl_rules = []

    def block_ip(self, ip_text, reason="manual_block", duration_seconds=None):
        ip_obj = _to_ip(ip_text)
        if ip_obj is None:
            return False
        now = time.time()
        with self._lock:
            self._set_temporary_block_locked(str(ip_obj), str(reason), now, duration_seconds=duration_seconds)
        return True

    def unblock_ip(self, ip_text):
        ip_obj = _to_ip(ip_text)
        if ip_obj is None:
            return False
        with self._lock:
            existed = str(ip_obj) in self._temporary_blocks
            self._temporary_blocks.pop(str(ip_obj), None)
        return existed

    def is_whitelisted(self, ip_text):
        with self._lock:
            return _ip_in_networks(ip_text, self._whitelist_networks)

    def is_blacklisted(self, ip_text):
        with self._lock:
            return _ip_in_networks(ip_text, self._blacklist_networks)

    def evaluate(self, request):
        now = time.time()
        ip_text = self.resolve_client_ip(request)
        with self._lock:
            self._expire_blocks_locked(now)
            self._prune_events_locked(self._request_events, now, self.rate_limit_window_seconds)
            self._prune_events_locked(self._violation_events, now, self.violation_window_seconds)
            self._prune_events_locked(self._suspicious_events, now, self.suspicious_window_seconds)
            self._requests_total += 1

            if ip_text:
                active_block = self._temporary_blocks.get(ip_text)
                if active_block:
                    retry_after = max(1, int(float(active_block.get("until", now)) - now))
                    reason = str(active_block.get("reason", "temporary_block"))
                    return self._deny_locked(
                        ip_text=ip_text,
                        now=now,
                        status=429,
                        reason=reason,
                        message="Too Many Requests",
                        retry_after=retry_after,
                    )

            whitelisted = bool(ip_text and _ip_in_networks(ip_text, self._whitelist_networks))
            blacklisted = bool(ip_text and _ip_in_networks(ip_text, self._blacklist_networks))

            if blacklisted and not (whitelisted and self.whitelist_overrides_blacklist):
                return self._deny_locked(
                    ip_text=ip_text,
                    now=now,
                    status=403,
                    reason="blacklist",
                    message="Forbidden",
                )

            enforce_behavior = not (whitelisted and self.whitelist_bypass_behavior)
            if (
                enforce_behavior
                and ip_text
                and self.rate_limit_requests > 0
                and self.rate_limit_window_seconds > 0
            ):
                requests_count = self._push_event_locked(
                    self._request_events,
                    ip_text,
                    now,
                    self.rate_limit_window_seconds,
                )
                if requests_count > self.rate_limit_requests:
                    until = self._set_temporary_block_locked(ip_text, "rate_limit", now)
                    retry_after = max(1, int(until - now))
                    return self._deny_locked(
                        ip_text=ip_text,
                        now=now,
                        status=429,
                        reason="rate_limit",
                        message="Too Many Requests",
                        retry_after=retry_after,
                    )

            matched_rule = None
            for rule in self._acl_rules:
                if rule.matches(request, ip_text):
                    matched_rule = rule
                    break

            if matched_rule:
                if matched_rule.effect == "deny":
                    self._acl_deny_total += 1
                    message = "Forbidden"
                    if matched_rule.name:
                        message = f"Forbidden ({matched_rule.name})"
                    return self._deny_locked(
                        ip_text=ip_text,
                        now=now,
                        status=403,
                        reason="acl_deny",
                        message=message,
                        rule_name=matched_rule.name,
                    )
                self._acl_allow_total += 1
                return self._allow_locked(ip_text)

            if self.acl_default == "deny":
                return self._deny_locked(
                    ip_text=ip_text,
                    now=now,
                    status=403,
                    reason="acl_default_deny",
                    message="Forbidden",
                )

            return self._allow_locked(ip_text)

    def observe_response(self, request, status_code):
        ip_text = self.resolve_client_ip(request)
        if not ip_text:
            return
        status = _safe_int(status_code, 0)
        if status <= 0:
            return
        now = time.time()
        with self._lock:
            self._expire_blocks_locked(now)
            whitelisted = _ip_in_networks(ip_text, self._whitelist_networks)
            if whitelisted and self.whitelist_bypass_behavior:
                return
            if status in self.suspicious_status_codes:
                self._register_suspicious_response_locked(ip_text, now)

    def snapshot(self):
        now = time.time()
        with self._lock:
            self._expire_blocks_locked(now)
            self._prune_events_locked(self._request_events, now, self.rate_limit_window_seconds)
            self._prune_events_locked(self._violation_events, now, self.violation_window_seconds)
            self._prune_events_locked(self._suspicious_events, now, self.suspicious_window_seconds)
            active_blocks = {
                ip: {
                    "reason": str(row.get("reason", "")),
                    "until_unix": round(float(row.get("until", 0.0)), 3),
                    "remaining_seconds": max(0, round(float(row.get("until", now)) - now, 3)),
                }
                for ip, row in self._temporary_blocks.items()
            }
            data = {
                "enabled": True,
                "policy": {
                    "acl_default": self.acl_default,
                    "trust_x_forwarded_for": self.trust_x_forwarded_for,
                    "whitelist_overrides_blacklist": self.whitelist_overrides_blacklist,
                    "whitelist_bypass_behavior": self.whitelist_bypass_behavior,
                    "rate_limit_requests": self.rate_limit_requests,
                    "rate_limit_window_seconds": self.rate_limit_window_seconds,
                    "violation_threshold": self.violation_threshold,
                    "violation_window_seconds": self.violation_window_seconds,
                    "suspicious_status_codes": sorted(self.suspicious_status_codes),
                    "suspicious_threshold": self.suspicious_threshold,
                    "suspicious_window_seconds": self.suspicious_window_seconds,
                    "block_duration_seconds": self.block_duration_seconds,
                },
                "lists": {
                    "whitelist": [str(n) for n in self._whitelist_networks],
                    "blacklist": [str(n) for n in self._blacklist_networks],
                },
                "counters": {
                    "requests_total": self._requests_total,
                    "allowed_total": self._allowed_total,
                    "blocked_total": self._blocked_total,
                    "acl_allow_total": self._acl_allow_total,
                    "acl_deny_total": self._acl_deny_total,
                    "temporary_blocks_total": self._temporary_blocks_total,
                    "temporary_blocks_active": len(self._temporary_blocks),
                    "blocked_by_reason": dict(self._blocked_by_reason),
                    "request_clients_tracked": len(self._request_events),
                    "violation_clients_tracked": len(self._violation_events),
                    "suspicious_clients_tracked": len(self._suspicious_events),
                },
                "acl": {
                    "rules_total": len(self._acl_rules),
                    "rules": [rule.describe() for rule in self._acl_rules],
                },
                "active_blocks": active_blocks,
            }
        return data


def install_security(app, policy=None):
    resolved = policy or SecurityPolicy()
    app.security = resolved
    return resolved


__all__ = [
    "ACLRule",
    "SecurityDecision",
    "SecurityPolicy",
    "DEFAULT_BLOCK_DURATION_SECONDS",
    "DEFAULT_RATE_LIMIT_REQUESTS",
    "DEFAULT_RATE_LIMIT_WINDOW_SECONDS",
    "DEFAULT_SUSPICIOUS_THRESHOLD",
    "DEFAULT_SUSPICIOUS_WINDOW_SECONDS",
    "DEFAULT_VIOLATION_THRESHOLD",
    "DEFAULT_VIOLATION_WINDOW_SECONDS",
    "install_security",
]
