# wsbuilder

`wsbuilder` es un paquete Python extraido de `PortHound4` para construir servidores HTTP + WebSocket sin dependencias externas.

Lightweight Python HTTP + WebSocket framework for building real-time APIs and custom servers.

**Keywords:** `python`, `http-server`, `websocket`, `framework`, `real-time`, `api`, `socket`.

## Incluye

- `wsbuilder.framework`: router, request/response, servidor HTTP, handshake WS y utilidades de frames.
- `wsbuilder.ws_demo`: demo completo HTTP + WebSocket + REST + SQLite.

## Instalacion local

```bash
python -m pip install -e .
```

## Uso rapido del framework

```python
from wsbuilder import App, Response, parse_close_payload

app = App()

@app.view("/")
def home(_request):
    return Response.html("<h1>wsbuilder</h1>")

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

app.run("0.0.0.0", 8765)
```

## Ejecutar demo incluido

```bash
python -m wsbuilder --host 0.0.0.0 --port 8765
# o
wsbuilder --host 0.0.0.0 --port 8765
```

## Variables de entorno

- `WSBUILDER_CORS_ALLOW_ORIGIN`: valor CORS para rutas `@app.api`.
- Compatibilidad heredada: tambien respeta `PORTHOUND_CORS_ALLOW_ORIGIN`.
