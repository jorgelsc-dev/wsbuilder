# WebSocket

WSBuilder incluye handshake, framing y una clase `WebSocket` para manejar la conexion ya abierta.

## Registro

```python
@app.ws(
    "/ws/",
    subprotocols=("json", "msgpack"),
    idle_timeout=15.0,
    keepalive_interval=5.0,
    pong_timeout=3.0,
)
def handler(ws, _request):
    ...
```

## Handshake

La funcion `handshake_websocket_with_options()` valida:

- `Sec-WebSocket-Key`
- `Connection: Upgrade`
- `Upgrade: websocket`
- `Sec-WebSocket-Version: 13`

Tambien negocia subprotocolos compatibles cuando se declaran en `supported_subprotocols`.

## `WebSocket`

Metodos mas utiles:

- `recv_frame()`
- `send_text(text)`
- `send_binary(data)`
- `send_ping(payload=b"")`
- `send_pong(payload=b"")`
- `send_frame(opcode, payload=b"")`
- `close(code=1000, reason="")`

Callbacks opcionales:

- `on_close`
- `on_error`
- `on_timeout`

## Ciclo de vida

`WebSocket` puede cerrar por:

- frame de cierre remoto
- timeout de inactividad
- fallo de ping/pong
- error de protocolo

## Ejemplo echo

```python
from wsbuilder import App

app = App()

@app.ws("/ws/")
def echo(ws, _request):
    while True:
        frame = ws.recv_frame()
        if frame.opcode == 0x8:
            ws.close(1000, "bye")
            break
        if frame.opcode == 0x9:
            ws.send_pong(frame.payload)
            continue
        if frame.opcode == 0x1:
            ws.send_text(frame.payload.decode("utf-8", errors="ignore"))
```

## Consejos

- Usa `subprotocols` si tu cliente exige un formato concreto.
- Activa `keepalive_interval` y `pong_timeout` en conexiones largas.
- Mantener `auto_pong=True` simplifica clientes sencillos.

## Utilidades relacionadas

- `parse_close_payload()` para interpretar el frame final.
- `make_ws_frame_bytes()` y `read_ws_frame_raw()` si necesitas trabajar a nivel bajo.
- `is_ws_request()` para checks rapidos de upgrade.
