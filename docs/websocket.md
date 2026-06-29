# WebSocket

## Registro de rutas WS

Las rutas WebSocket se registran con `@app.ws(path, ...)`.

```python
@app.ws("/ws/echo", keepalive_interval=30.0, pong_timeout=10.0)
def ws_echo(ws, _request):
    while True:
        frame = ws.recv_frame()
        if frame.opcode == 0x1:
            ws.send_text(frame.payload.decode("utf-8", errors="ignore"))
        elif frame.opcode == 0x8:
            ws.close(1000, "bye")
            break
```

## Opciones del decorador

| Opcion | Uso |
| --- | --- |
| `subprotocols` | lista de subprotocolos soportados |
| `idle_timeout` | cierre por inactividad |
| `keepalive_interval` | envio automatico de ping |
| `pong_timeout` | tiempo maximo esperando pong |
| `auto_pong` | respuesta automatica a ping |
| `on_close` | callback al cerrar |
| `on_error` | callback en error |
| `on_timeout` | callback en timeout |
| `io_poll_interval` | granularidad de espera de I/O |
| `ping_payload` | payload fijo para pings de keepalive |

## `WebSocket`

Metodos publicos mas usados:

- `recv_frame()`
- `send_frame(opcode, payload)`
- `send_text(text)`
- `send_binary(data)`
- `send_ping(payload=b"")`
- `send_pong(payload=b"")`
- `close(code=1000, reason="")`

`recv_frame()` devuelve un `WebSocketFrame` con `opcode`, `payload`, `fin` y
metadatos del frame leido.

## Handshake y utilidades

El modulo `ws.py` tambien expone helpers de bajo nivel:

- `is_ws_request(headers)`
- `handshake_websocket(...)`
- `handshake_websocket_with_options(...)`
- `read_ws_frame_raw(conn)`
- `make_ws_frame_bytes(opcode, payload=b"")`
- `parse_close_payload(payload)`
- `recv_exact(conn, n)`

Son utiles cuando quieres inspeccionar o reutilizar piezas del protocolo sin
pasar por `App`.

## Buenas practicas

- Usa `keepalive_interval` y `pong_timeout` cuando el cliente puede quedar
  mucho tiempo ocioso.
- Trata `opcode == 0x8` como cierre ordenado.
- Para mensajes de texto, decodifica con tolerancia a errores si el origen no
  esta completamente controlado.
- Si necesitas limitar trafico o ACL, habilita `SecurityPolicy` tambien en la
  aplicacion HTTP; el handshake pasa por la evaluacion de seguridad.
