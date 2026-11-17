"""The ProxyI reverse proxy: dispatch, balancing, dashboard and installation."""

from __future__ import annotations

import http.client
import json
import threading
import time
from html import escape as html_escape

from .._coerce import safe_float as _safe_float
from .._coerce import safe_int as _safe_int
from ..http import Response
from ._common import (
    BALANCING_ROUND_ROBIN,
    DEFAULT_DASHBOARD_PATH,
    DEFAULT_MAX_REQUEST_BODY_BYTES,
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_METRICS_PATH,
    DEFAULT_RESPONSE_TIMEOUT_SECONDS,
    DEFAULT_STREAM_PATH,
    DEFAULT_STREAM_POINTS,
    SUPPORTED_BALANCING_STRATEGIES,
    _clean_request_headers,
    _clean_response_headers,
    _collect_upstream_headers,
    _extract_request_host,
    _forwarded_port_value,
    _has_dot_segments,
    _header_value,
    _is_truthy,
    _iter_upstream_body,
    _normalize_text,
    _read_upstream_body,
    _request_body_bytes,
    _request_path,
    _request_query_string,
    _should_stream_upstream,
    _UpstreamResponseTimeout,
    _UpstreamResponseTooLarge,
    normalize_balance_mode,
)
from .routing import (
    ProxyRouteBuilder,
    ProxyRule,
)
from .stats import (
    ProxyMetricsBucket,
)


class ProxyI:
    def __init__(
        self,
        *,
        name="proxyi",
        dashboard_path=DEFAULT_DASHBOARD_PATH,
        metrics_path=DEFAULT_METRICS_PATH,
        metrics_stream_path=DEFAULT_STREAM_PATH,
        default_balance=BALANCING_ROUND_ROBIN,
        max_request_body_bytes=DEFAULT_MAX_REQUEST_BODY_BYTES,
        max_response_bytes=DEFAULT_MAX_RESPONSE_BYTES,
        response_timeout_seconds=DEFAULT_RESPONSE_TIMEOUT_SECONDS,
        expose_upstream_header=False,
        guard=None,
        access=None,
        token=None,
        authorize=None,
    ):
        from .._access import coerce_guard

        #: Who may read the dashboard and metrics (private networks by default).
        self.guard = coerce_guard(guard, access=access, token=token, authorize=authorize)
        self.name = _normalize_text(name) or "proxyi"
        self.dashboard_path = _normalize_text(dashboard_path) or DEFAULT_DASHBOARD_PATH
        self.metrics_path = _normalize_text(metrics_path) or DEFAULT_METRICS_PATH
        self.metrics_stream_path = _normalize_text(metrics_stream_path) or DEFAULT_STREAM_PATH
        self.default_balance = normalize_balance_mode(default_balance)
        self.max_request_body_bytes = max(0, _safe_int(max_request_body_bytes, DEFAULT_MAX_REQUEST_BODY_BYTES))
        self.max_response_bytes = max(0, _safe_int(max_response_bytes, DEFAULT_MAX_RESPONSE_BYTES))
        self.response_timeout_seconds = max(
            0.1, _safe_float(response_timeout_seconds, DEFAULT_RESPONSE_TIMEOUT_SECONDS)
        )
        #: Adds ``X-ProxyI-Upstream`` (the internal upstream URL) to responses.
        self.expose_upstream_header = bool(expose_upstream_header)
        self.started_at = time.time()
        self.metrics = ProxyMetricsBucket(label=self.name)
        self._rules = []
        self._default_rule = None
        self._lock = threading.RLock()
        self._order = 0
        self._app = None

    def _next_order(self):
        with self._lock:
            self._order += 1
            return self._order

    def routes(self):
        with self._lock:
            return tuple(self._rules)

    def _rule_matches(self, rule, request):
        return rule.matches(request)

    def _resolve_rules(self, request):
        with self._lock:
            rows = [rule for rule in self._rules if self._rule_matches(rule, request)]
            if rows:
                rows.sort(key=lambda row: (-int(row.priority), -int(row.specificity()), int(row.order)))
                return rows
            if self._default_rule is not None:
                return [self._default_rule]
        return []

    def _fallback_target(self, request):
        with self._lock:
            if self._default_rule is None:
                return None
            return self._default_rule.choose_target(request, proxy=None)

    def add_rule(self, rule):
        if not isinstance(rule, ProxyRule):
            raise TypeError("add_rule expects a ProxyRule instance")
        with self._lock:
            if rule.order <= 0:
                rule.order = self._next_order()
            if rule.default:
                self._default_rule = rule
            if rule not in self._rules:
                self._rules.append(rule)
        return rule

    def route(self, **kwargs):
        kwargs.setdefault("balance", self.default_balance)
        return ProxyRouteBuilder(self, **kwargs)

    def vhost(self, *hosts, **kwargs):
        kwargs["hosts"] = tuple(hosts)
        return self.route(**kwargs)

    def location(self, path, **kwargs):
        kwargs["path_prefix"] = path
        return self.route(**kwargs)

    def default(self, *targets, **kwargs):
        kwargs["default"] = True
        builder = self.route(**kwargs)
        for target in targets:
            builder.upstream(target)
        return builder

    def install(self, app=None, *, metrics_path=None, stream_path=None, dashboard_path=None):
        return install_proxyi(
            app or self._app,
            proxy=self,
            metrics_path=metrics_path or self.metrics_path,
            stream_path=stream_path or self.metrics_stream_path,
            dashboard_path=dashboard_path or self.dashboard_path,
        )

    def _record_rule_start(self, rule, request_size):
        if rule is not None:
            rule.metrics.begin_request(request_size)

    def _record_rule_finish(self, rule, status, response_size, latency_ms, error=None):
        if rule is not None:
            rule.metrics.finish_request(status, response_size=response_size, latency_ms=latency_ms, error=error)

    def _record_target_start(self, target, request_size):
        if target is not None:
            target.metrics.begin_request(request_size)
            target.metrics.mark_selected()

    def _record_target_finish(self, target, status, response_size, latency_ms, error=None):
        if target is not None:
            target.metrics.finish_request(status, response_size=response_size, latency_ms=latency_ms, error=error)

    def _forward_request(self, request, rule, target):
        body = _request_body_bytes(request)
        query_string = _request_query_string(request)
        request_path = _request_path(request)
        strip_prefix = rule.path_prefix if (rule and rule.strip_prefix) else ""
        forward_path = target.build_forward_path(
            request_path,
            query_string=query_string,
            strip_prefix=strip_prefix,
        )
        headers = _clean_request_headers(getattr(request, "headers", {}) or {})
        headers.update(target.extra_headers)
        headers["X-Forwarded-For"] = self._forwarded_for_value(request)
        headers["X-Forwarded-Host"] = _header_value(request, "host") or _extract_request_host(request)
        headers["X-Forwarded-Proto"] = "https" if getattr(request, "tls", {}).get("enabled") else "http"
        headers["X-Forwarded-Port"] = _forwarded_port_value(request)
        headers["X-ProxyI-Rule"] = rule.name or rule.path_prefix or rule.path or "default"
        headers["X-ProxyI-Target"] = target.name
        for header_name in list(headers):
            if header_name.lower() == "host":
                headers.pop(header_name, None)
        preserve_host = bool(target.preserve_host)
        if rule is not None:
            preserve_host = preserve_host and bool(rule.preserve_host)
        if preserve_host and _header_value(request, "host"):
            headers["Host"] = _header_value(request, "host")
        else:
            headers["Host"] = target.authority

        conn = target.open_connection()
        streaming = False
        try:
            conn.request(request.method, forward_path, body=body, headers=headers)
            upstream = conn.getresponse()
            status = int(getattr(upstream, "status", 502) or 502)
            reason = _normalize_text(getattr(upstream, "reason", "") or "")
            raw_headers = _collect_upstream_headers(upstream.getheaders())
            response_headers = _clean_response_headers(raw_headers)
            response_headers.setdefault("X-ProxyI-Rule", rule.name or rule.path_prefix or rule.path or "default")
            response_headers.setdefault("X-ProxyI-Target", target.name)
            if self.expose_upstream_header:
                response_headers.setdefault("X-ProxyI-Upstream", target.url)
            has_body = request.method.upper() != "HEAD" and not (
                100 <= status < 200 or status in {204, 304}
            )
            if has_body and _should_stream_upstream(upstream, raw_headers):
                # Unbounded or event streams are relayed as they arrive: each
                # read is bounded by the target's socket timeout.
                for header_name in list(response_headers):
                    if header_name.lower() == "content-length":
                        response_headers.pop(header_name)
                streaming = True
                return Response(
                    status=status,
                    headers=response_headers,
                    reason=reason,
                    stream=_iter_upstream_body(upstream, conn),
                )
            raw_body = b""
            if has_body:
                deadline = time.monotonic() + self.response_timeout_seconds
                try:
                    raw_body = _read_upstream_body(
                        upstream, max_bytes=self.max_response_bytes, deadline=deadline
                    )
                except _UpstreamResponseTooLarge:
                    return Response.text("Bad Gateway: upstream response too large", status=502)
                except _UpstreamResponseTimeout:
                    return Response.text("Gateway Timeout", status=504)
                except http.client.IncompleteRead:
                    return Response.text("Bad Gateway: incomplete upstream response", status=502)
            if not any(
                name.lower() == "content-length"
                for name in response_headers
            ):
                response_headers["Content-Length"] = str(len(raw_body))
            return Response(status=status, body=raw_body, headers=response_headers, reason=reason)
        finally:
            if not streaming:
                try:
                    conn.close()
                except OSError:
                    pass  # Closing an upstream that already dropped is harmless.

    def _forwarded_for_value(self, request):
        client = getattr(request, "client", None)
        ip = ""
        if client and isinstance(client, (list, tuple)) and client:
            ip = _normalize_text(client[0])
        return ip

    def dispatch(self, request):
        path = _request_path(request)
        method = _normalize_text(getattr(request, "method", "GET")).upper() or "GET"
        if path == self.metrics_path:
            if method != "GET":
                return Response.text("Method Not Allowed", status=405, headers={"Allow": "GET"})
            return self.guarded(request, self.response_snapshot)
        if path == self.metrics_stream_path:
            if method != "GET":
                return Response.text("Method Not Allowed", status=405, headers={"Allow": "GET"})
            interval = getattr(request, "query", {}).get("interval", "1.0")
            limit_text = getattr(request, "query", {}).get("limit", "")
            follow = getattr(request, "query", {}).get("follow", "")
            if limit_text:
                parsed = _safe_int(limit_text, DEFAULT_STREAM_POINTS)
                max_points = None if parsed <= 0 else parsed
            else:
                max_points = None if _is_truthy(follow) else DEFAULT_STREAM_POINTS
            return self.guarded_stream(request, interval_seconds=interval, max_points=max_points)
        if path == self.dashboard_path:
            if method != "GET":
                return Response.text("Method Not Allowed", status=405, headers={"Allow": "GET"})
            return self.guarded(request, self.response_dashboard)

        if _has_dot_segments(path):
            # Rules match on the raw path, so a dot segment the upstream may
            # normalize away could escape a prefix rule or the target base path.
            return Response.text("Bad Request: dot segments are not allowed", status=400)

        request_size = len(_request_body_bytes(request))
        started = time.time()
        self.metrics.begin_request(request_size)

        rule = None
        target = None
        response = None
        error = None
        try:
            if request_size > self.max_request_body_bytes:
                response = Response.text("Payload Too Large", status=413)
                return response
            matches = self._resolve_rules(request)
            rule = matches[0] if matches else None
            if rule is None:
                response = Response.text("Not Found", status=404)
                return response
            rule.metrics.mark_selected()
            self._record_rule_start(rule, request_size)
            target = rule.choose_target(request, proxy=self)
            if target is None:
                response = Response.text("Bad Gateway", status=502)
                error = "no upstream target"
                return response
            self._record_target_start(target, request_size)
            response = self._forward_request(request, rule, target)
            return response
        except Exception as exc:
            error = exc
            response = Response.text("Bad Gateway", status=502)
            return response
        finally:
            elapsed_ms = (time.time() - started) * 1000.0
            status = response.status if response is not None else 502
            body_size = len(response.body) if response is not None and not response.is_stream else 0
            self.metrics.finish_request(status, response_size=body_size, latency_ms=elapsed_ms, error=error)
            self._record_rule_finish(rule, status, body_size, elapsed_ms, error=error)
            self._record_target_finish(target, status, body_size, elapsed_ms, error=error)

    def metrics_snapshot(self):
        now = time.time()
        summary = self.metrics.snapshot()
        with self._lock:
            rules = [rule.snapshot(include_metrics=True) for rule in self._rules]
            targets = []
            for rule in self._rules:
                for target in rule.targets:
                    targets.append(target.snapshot(include_metrics=True))
            total_targets = len(targets)
        best_target = None
        if targets:
            ranked = sorted(targets, key=lambda row: (row.get("score", float("inf")), row.get("priority", 0), row.get("name", "")))
            best_target = ranked[0]
        return {
            "name": self.name,
            "started_at_unix": self.started_at,
            "uptime_seconds": round(now - self.started_at, 3),
            "dashboard_path": self.dashboard_path,
            "metrics_path": self.metrics_path,
            "metrics_stream_path": self.metrics_stream_path,
            "default_balance": self.default_balance,
            "supported_balancing": list(SUPPORTED_BALANCING_STRATEGIES),
            "rules_total": len(rules),
            "targets_total": total_targets,
            "rules": rules,
            "targets": targets,
            "best_target": best_target,
            "summary": summary,
        }

    def snapshot(self):
        return self.metrics_snapshot()

    def describe(self):
        return self.snapshot()

    def stream_chunks(self, interval_seconds=1.0, max_points=None):
        interval_seconds = _safe_float(interval_seconds, 1.0)
        if interval_seconds < 0.1:
            interval_seconds = 0.1
        if interval_seconds > 60.0:
            interval_seconds = 60.0
        if max_points is not None:
            max_points = _safe_int(max_points, None)
            if max_points is not None and max_points <= 0:
                max_points = None
        sent = 0
        while True:
            payload = json.dumps(
                self.snapshot(),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8") + b"\n"
            yield payload
            sent += 1
            if max_points is not None and sent >= max_points:
                break
            time.sleep(interval_seconds)

    def response_snapshot(self):
        return Response.json(self.snapshot())

    def guarded(self, request, build):
        """Run ``build()`` when ``request`` passes the guard, else refuse it."""
        refusal = self.guard.check(request, getattr(self, "_app", None))
        if refusal is not None:
            return refusal
        return build()

    def guarded_stream(self, request, interval_seconds=1.0, max_points=None):
        refusal = self.guard.check(request, getattr(self, "_app", None))
        if refusal is not None:
            return refusal
        if not self.guard.acquire_stream():
            return self.guard.too_many_streams()
        return self.response_stream(
            interval_seconds=interval_seconds, max_points=max_points, guard=self.guard
        )

    def response_stream(self, interval_seconds=1.0, max_points=None, guard=None):
        headers = {
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-Accel-Buffering": "no",
            "X-Content-Type-Options": "nosniff",
        }
        chunks = self.stream_chunks(interval_seconds=interval_seconds, max_points=max_points)
        if guard is not None:
            chunks = guard.bounded(chunks)
        return Response.stream(
            chunks,
            content_type="application/x-ndjson; charset=utf-8",
            headers=headers,
        )

    def render_dashboard_html(self, snapshot=None):
        data = snapshot or self.snapshot()
        summary = data.get("summary") or {}
        rules = list(data.get("rules") or [])
        targets = list(data.get("targets") or [])
        best_target = data.get("best_target") or {}

        def render_card(label, value, hint=""):
            return f"""
            <article class="card">
              <div class="k">{html_escape(str(label))}</div>
              <div class="v">{html_escape(str(value))}</div>
              <div class="hint">{html_escape(str(hint))}</div>
            </article>
            """

        def fmt_stat(stat):
            stat = stat or {}
            return (
                f"n={stat.get('count', 0)} mean={stat.get('mean', 0)} "
                f"std={stat.get('stddev', 0)} ci95=±{stat.get('uncertainty_95', 0)}"
            )

        rule_rows = []
        for row in rules:
            match_bits = []
            if row.get("hosts"):
                match_bits.append(f"hosts={', '.join(row.get('hosts') or [])}")
            if row.get("path"):
                match_bits.append(f"path={row.get('path')}")
            if row.get("path_prefix"):
                match_bits.append(f"prefix={row.get('path_prefix')}")
            if row.get("header_equals"):
                match_bits.append(f"headers={json.dumps(row.get('header_equals'), ensure_ascii=False)}")
            targets_preview = ", ".join(target.get("name", "") for target in row.get("targets", [])) or "-"
            rule_rows.append(
                "<tr>"
                f"<td data-label=\"Name\"><code>{html_escape(str(row.get('name') or '-'))}</code></td>"
                f"<td data-label=\"Match\">{html_escape('; '.join(match_bits) or '-')}</td>"
                f"<td data-label=\"Balance\">{html_escape(str(row.get('balance') or '-'))}</td>"
                f"<td data-label=\"Targets\">{html_escape(targets_preview)}</td>"
                f"<td data-label=\"Metrics\">{html_escape(str(row.get('metrics', {}).get('requests_total', 0)))}</td>"
                "</tr>"
            )

        target_rows = []
        for row in targets:
            metrics = row.get("metrics") or {}
            target_rows.append(
                "<tr>"
                f"<td data-label=\"Target\"><code>{html_escape(str(row.get('name') or '-'))}</code></td>"
                f"<td data-label=\"URL\"><code>{html_escape(str(row.get('url') or '-'))}</code></td>"
                f"<td data-label=\"Load\">{html_escape(str(metrics.get('current_load', 0)))}</td>"
                f"<td data-label=\"Latency\">{html_escape(str(metrics.get('latency_ms', {}).get('mean', 0)))}</td>"
                f"<td data-label=\"Req size\">{html_escape(str(metrics.get('request_bytes', {}).get('mean', 0)))}</td>"
                f"<td data-label=\"Resp size\">{html_escape(str(metrics.get('response_bytes', {}).get('mean', 0)))}</td>"
                f"<td data-label=\"Errors\">{html_escape(str(metrics.get('errors_total', 0)))}</td>"
                f"<td data-label=\"Score\">{html_escape(str(row.get('score', 0)))}</td>"
                "</tr>"
            )

        stats_rows = [
            ("requests_total", summary.get("requests_total", 0)),
            ("responses_total", summary.get("responses_total", 0)),
            ("errors_total", summary.get("errors_total", 0)),
            ("active_requests", summary.get("active_requests", 0)),
            ("bytes_in_total", summary.get("bytes_in_total", 0)),
            ("bytes_out_total", summary.get("bytes_out_total", 0)),
        ]

        return f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html_escape(self.name)} metrics</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f7f2;
      --panel: #ffffff;
      --ink: #111827;
      --muted: #5b6472;
      --line: #d7d9df;
      --brand: #0f766e;
      --accent: #ca8a04;
      --shadow: 0 18px 40px rgba(17, 24, 39, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at 20% 20%, rgba(15,118,110,0.12), transparent 28%),
        radial-gradient(circle at 80% 0%, rgba(202,138,4,0.12), transparent 22%),
        linear-gradient(180deg, #fff, var(--bg));
    }}
    a {{ color: var(--brand); text-decoration: none; }}
    .wrap {{ max-width: 1320px; margin: 0 auto; padding: 28px 18px 56px; }}
    .hero {{
      background: linear-gradient(135deg, rgba(15,118,110,0.12), rgba(202,138,4,0.08));
      border: 1px solid var(--line);
      border-radius: 26px;
      box-shadow: var(--shadow);
      padding: 24px;
      margin-bottom: 20px;
    }}
    .hero h1 {{
      margin: 0;
      font-size: clamp(2rem, 4vw, 3.3rem);
      letter-spacing: -0.04em;
      line-height: 1.03;
    }}
    .hero p {{ margin: 10px 0 0; color: var(--muted); max-width: 72ch; }}
    .grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin: 18px 0 24px; }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 16px;
      box-shadow: 0 8px 18px rgba(17, 24, 39, 0.05);
    }}
    .k {{ color: var(--muted); font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.08em; font-weight: 700; }}
    .v {{ margin-top: 8px; font-size: 1.7rem; font-weight: 800; letter-spacing: -0.03em; }}
    .hint {{ margin-top: 8px; color: var(--muted); font-size: 0.88rem; line-height: 1.4; }}
    .section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 24px;
      box-shadow: var(--shadow);
      overflow: hidden;
      margin-bottom: 18px;
    }}
    .section h2 {{ margin: 0; padding: 18px 20px 8px; font-size: 1.15rem; }}
    .section p.lead {{ margin: 0; padding: 0 20px 16px; color: var(--muted); }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ text-align: left; vertical-align: top; padding: 12px 20px; border-top: 1px solid var(--line); word-break: break-word; }}
    th {{
      background: rgba(15, 118, 110, 0.06);
      font-size: 0.82rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}
    code {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      background: rgba(17, 24, 39, 0.06);
      padding: 0.12em 0.35em;
      border-radius: 0.35rem;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(202, 138, 4, 0.12);
      color: #7c4a00;
      font-weight: 700;
      font-size: 0.88rem;
    }}
    @media (max-width: 960px) {{
      .grid {{ grid-template-columns: 1fr; }}
      table, thead, tbody, tr, th, td {{ display: block; }}
      thead {{ display: none; }}
      tr {{ border-top: 1px solid var(--line); }}
      td {{ border-top: 0; }}
      td::before {{
        content: attr(data-label);
        display: block;
        margin-bottom: 4px;
        color: var(--muted);
        font-size: 0.76rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        font-weight: 700;
      }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <header class="hero">
      <div class="badge">ProxyI metrics area</div>
      <h1>{html_escape(self.name)}</h1>
      <p>Reglas tipo vhost/location, balanceo adaptativo y estadisticas por destino. Los datos que aparecen aqui salen del motor activo y pueden usarse para elegir el mejor upstream por latencia, carga o estabilidad.</p>
    </header>

    <section class="grid" aria-label="Resumen">
      {render_card("requests", summary.get("requests_total", 0), "Requests procesadas por el proxy")}
      {render_card("responses", summary.get("responses_total", 0), "Respuestas emitidas al cliente")}
      {render_card("errors", summary.get("errors_total", 0), "Errores de transporte o upstream")}
    </section>

    <section class="grid" aria-label="Cargas">
      {render_card("active", summary.get("active_requests", 0), "Carga concurrente actual")}
      {render_card("bytes in", summary.get("bytes_in_total", 0), "Tamano acumulado de peticiones")}
      {render_card("bytes out", summary.get("bytes_out_total", 0), "Tamano acumulado de respuestas")}
    </section>

    <section class="section">
      <h2>Estado operacional</h2>
      <p class="lead">El panel muestra la distribucion actual y una recomendacion de destino basada en la metrica agregada.</p>
      <table>
        <tbody>
          <tr><th>Campo</th><th>Valor</th></tr>
          <tr><td data-label="Campo">Balance por defecto</td><td data-label="Valor"><code>{html_escape(str(data.get("default_balance") or "-"))}</code></td></tr>
          <tr><td data-label="Campo">Rutas</td><td data-label="Valor">{html_escape(str(data.get("rules_total", 0)))}</td></tr>
          <tr><td data-label="Campo">Destinos</td><td data-label="Valor">{html_escape(str(data.get("targets_total", 0)))}</td></tr>
          <tr><td data-label="Campo">Mejor destino</td><td data-label="Valor"><code>{html_escape(str((best_target or {}).get("name") or "-"))}</code></td></tr>
        </tbody>
      </table>
    </section>

    <section class="section">
      <h2>Metricas base</h2>
      <p class="lead">Media, desviacion estandar, incertidumbre y rango de confianza al 95% por destino.</p>
      <table>
        <thead>
          <tr>
            <th>Metrica</th>
            <th>Valor</th>
          </tr>
        </thead>
        <tbody>
          {''.join(
            f'<tr><td data-label="Metrica">{html_escape(str(label))}</td><td data-label="Valor">{html_escape(str(value))}</td></tr>'
            for label, value in stats_rows
          )}
        </tbody>
      </table>
      <details style="border-top: 1px solid var(--line); padding: 0 20px 20px;">
        <summary style="cursor: pointer; padding: 14px 0; color: var(--brand); font-weight: 700;">Ver estadisticas detalladas del mejor destino</summary>
        <pre style="margin: 0; padding: 16px; border-radius: 16px; background: #0b1020; color: #e2e8f0; overflow: auto;">{html_escape(json.dumps(best_target, ensure_ascii=False, indent=2))}</pre>
      </details>
    </section>

    <section class="section">
      <h2>Reglas</h2>
      <p class="lead">Las reglas se ordenan por prioridad y especificidad para comportarse como un vhost de proxy.</p>
      <table>
        <thead>
          <tr>
            <th>Nombre</th>
            <th>Match</th>
            <th>Balance</th>
            <th>Targets</th>
            <th>Requests</th>
          </tr>
        </thead>
        <tbody>
          {''.join(rule_rows) or '<tr><td colspan="5" class="muted" style="padding:16px 20px;">No hay reglas registradas.</td></tr>'}
        </tbody>
      </table>
    </section>

    <section class="section">
      <h2>Destinos</h2>
      <p class="lead">Cada target conserva sus propias medias, desviacion, incertidumbre y score para permitir balanceo adaptativo.</p>
      <table>
        <thead>
          <tr>
            <th>Target</th>
            <th>URL</th>
            <th>Load</th>
            <th>Latency</th>
            <th>Request size</th>
            <th>Response size</th>
            <th>Errors</th>
            <th>Score</th>
          </tr>
        </thead>
        <tbody>
          {''.join(target_rows) or '<tr><td colspan="8" class="muted" style="padding:16px 20px;">No hay destinos registrados.</td></tr>'}
        </tbody>
      </table>
    </section>
  </div>
</body>
</html>"""

    def response_dashboard(self):
        return Response.html(self.render_dashboard_html())

    def close(self):
        self._rules = []
        self._default_rule = None

def install_proxyi(
    app,
    *,
    proxy=None,
    metrics_path=DEFAULT_METRICS_PATH,
    stream_path=DEFAULT_STREAM_PATH,
    dashboard_path=DEFAULT_DASHBOARD_PATH,
    guard=None,
    access=None,
    token=None,
    authorize=None,
):
    """Attach ``proxy`` (or a new ``ProxyI``) and its dashboard routes to ``app``.

    The dashboard and metrics answer only loopback and private networks
    unless ``access``/``token``/``authorize`` (or ``guard``) say otherwise.
    """
    if proxy is None:
        proxy = ProxyI(
            metrics_path=metrics_path,
            metrics_stream_path=stream_path,
            dashboard_path=dashboard_path,
            guard=guard,
            access=access,
            token=token,
            authorize=authorize,
        )
    elif guard is not None or access is not None or token is not None or authorize is not None:
        from .._access import coerce_guard

        proxy.guard = coerce_guard(guard, access=access, token=token, authorize=authorize)
    if app is None:
        return proxy

    app.proxyi = proxy
    proxy._app = app

    @app.api(metrics_path, methods=("GET",))
    def _proxy_metrics(request):
        return proxy.guarded(request, proxy.response_snapshot)

    @app.api(stream_path, methods=("GET",))
    def _proxy_metrics_stream(request):
        interval = request.query.get("interval", "1.0")
        limit_text = request.query.get("limit", "")
        follow = request.query.get("follow", "")
        if limit_text:
            parsed = _safe_int(limit_text, DEFAULT_STREAM_POINTS)
            max_points = None if parsed <= 0 else parsed
        else:
            max_points = None if _is_truthy(follow) else DEFAULT_STREAM_POINTS
        return proxy.guarded_stream(request, interval_seconds=interval, max_points=max_points)

    @app.view(dashboard_path, methods=("GET",))
    def _proxy_dashboard(request):
        return proxy.guarded(request, proxy.response_dashboard)

    return proxy

proxyi = ProxyI
