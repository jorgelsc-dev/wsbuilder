import argparse
import threading

from .framework import App, Response, parse_close_payload

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8765

MONITOR_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>wsbuilder monitor</title>
  <style>
    :root { color-scheme: light dark; }
    body { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; margin: 20px; }
    .row { display: flex; gap: 10px; align-items: center; margin-bottom: 12px; flex-wrap: wrap; }
    input, button { font: inherit; padding: 6px 8px; }
    #status { font-weight: 600; }
    .grid { display: grid; grid-template-columns: repeat(3, minmax(180px, 1fr)); gap: 10px; margin: 12px 0; }
    .card { border: 1px solid #6666; border-radius: 8px; padding: 10px; }
    .k { opacity: 0.75; font-size: 12px; }
    .v { font-size: 24px; margin-top: 6px; }
    pre { border: 1px solid #6666; border-radius: 8px; padding: 10px; overflow: auto; max-height: 48vh; }
  </style>
</head>
<body>
  <h2>WSBuilder Metrics Live (HTTP Streaming)</h2>
  <div class="row">
    <label>interval:
      <input id="interval" type="number" min="0.1" step="0.1" value="0.5" />
    </label>
    <button id="start">start</button>
    <button id="stop">stop</button>
    <span id="status">disconnected</span>
  </div>

  <div class="grid">
    <div class="card"><div class="k">timestamp</div><div class="v" id="ts">-</div></div>
    <div class="card"><div class="k">http inflight</div><div class="v" id="inflight">0</div></div>
    <div class="card"><div class="k">http requests total</div><div class="v" id="req_total">0</div></div>
    <div class="card"><div class="k">thread load total</div><div class="v" id="load_total">0</div></div>
    <div class="card"><div class="k">thread workers total</div><div class="v" id="workers_total">0</div></div>
    <div class="card"><div class="k">errors total</div><div class="v" id="errors_total">0</div></div>
  </div>

  <pre id="raw">waiting...</pre>

  <script>
    let controller = null;
    let connected = false;

    function setStatus(text) {
      document.getElementById("status").textContent = text;
    }

    function updateView(data) {
      document.getElementById("ts").textContent = data.timestamp_utc || "-";
      document.getElementById("inflight").textContent = data.connections?.http_inflight ?? 0;
      document.getElementById("req_total").textContent = data.http?.requests_total ?? 0;
      document.getElementById("load_total").textContent = data.threads?.current_load_total ?? 0;
      document.getElementById("workers_total").textContent = data.threads?.workers_total ?? 0;
      document.getElementById("errors_total").textContent = data.errors?.total ?? 0;
      document.getElementById("raw").textContent = JSON.stringify(data, null, 2);
    }

    async function startStream() {
      stopStream();
      const interval = Number(document.getElementById("interval").value || "0.5");
      const safeInterval = Number.isFinite(interval) && interval > 0 ? interval : 0.5;
      controller = new AbortController();
      setStatus("connecting");

      try {
        const url = `/api/metrics/stream?interval=${encodeURIComponent(safeInterval)}&follow=1`;
        const response = await fetch(url, { signal: controller.signal, cache: "no-store" });
        if (!response.ok || !response.body) {
          setStatus(`error: http ${response.status}`);
          return;
        }
        connected = true;
        setStatus("connected");

        const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
        let buffer = "";
        while (connected) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += value;
          const lines = buffer.split("\\n");
          buffer = lines.pop() || "";
          for (const line of lines) {
            const text = line.trim();
            if (!text) continue;
            try {
              const parsed = JSON.parse(text);
              updateView(parsed);
            } catch (_e) {
              // ignore malformed chunks
            }
          }
        }
      } catch (e) {
        if (controller && controller.signal.aborted) {
          setStatus("stopped");
          return;
        }
        setStatus(`error: ${e}`);
      } finally {
        connected = false;
      }
    }

    function stopStream() {
      connected = false;
      if (controller) {
        controller.abort();
        controller = null;
      }
      setStatus("disconnected");
    }

    document.getElementById("start").addEventListener("click", startStream);
    document.getElementById("stop").addEventListener("click", stopStream);
    startStream();
  </script>
</body>
</html>
"""


def build_demo_app():
    app = App()
    app.enable_metrics(app_name="wsbuilder-demo")

    @app.view("/")
    def home(_request):
        return Response.html(
            "<h1>wsbuilder</h1>"
            "<p>Demo core server running.</p>"
            "<p>Metrics: <code>/api/metrics</code> y <code>/api/metrics/stream</code>.</p>"
            "<p>Live monitor: <a href='/monitor'>/monitor</a>.</p>"
            "<p>Thread demo: <code>/thread-demo</code>.</p>"
        )

    @app.view("/monitor")
    def monitor(_request):
        return Response.html(MONITOR_HTML)

    @app.view("/thread-demo", min_threads=1, max_threads=4, requests_per_thread=0)
    def thread_demo(_request):
        return f"thread={threading.current_thread().name}"

    @app.api("/api/health")
    def health(_request):
        return {"ok": True}

    @app.ws("/ws/")
    def ws_handler(ws, _request):
        while True:
            fin, opcode, payload, _masked, _mask = ws.recv_frame()
            if opcode == 0x8:
                code, reason = parse_close_payload(payload)
                ws.close(code or 1000, reason or "")
                break
            if opcode == 0x9:
                ws.send_pong(payload)
                continue
            if opcode == 0x1:
                ws.send_text(payload.decode("utf-8", errors="ignore"))
            elif opcode == 0x2:
                ws.send_binary(payload)

    return app


def main():
    parser = argparse.ArgumentParser(
        description="Levanta un servidor demo HTTP + WebSocket de wsbuilder"
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="Host de escucha")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Puerto de escucha")
    args = parser.parse_args()

    app = build_demo_app()
    app.run(args.host, args.port)


if __name__ == "__main__":
    main()
