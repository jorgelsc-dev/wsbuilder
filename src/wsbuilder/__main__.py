import argparse

from .framework import App, Response, parse_close_payload

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8765


def build_demo_app():
    app = App()

    @app.view("/")
    def home(_request):
        return Response.html("<h1>wsbuilder</h1><p>Demo core server running.</p>")

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

