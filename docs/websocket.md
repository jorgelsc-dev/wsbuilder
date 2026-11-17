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
    max_message_bytes=256 * 1024,
)
def handler(ws, _request):
    ...
```

### Limites de tamano

| Opcion | Por defecto | Efecto |
| --- | --- | --- |
| `max_frame_bytes` | `2 MiB` | frame mas grande aceptado |
| `max_message_bytes` | `2 MiB` | mensaje reensamblado mas grande; tambien limita cada frame |

Si un cliente los supera, la conexion se cierra con el codigo `1009`
(*Message Too Big*) y `recv_frame()` lanza `WebSocketMessageTooBigError`
(subclase de `WebSocketProtocolError`). Para fijar el limite de todas las
rutas usa `HTTPServer(..., max_ws_frame_bytes=..., max_ws_message_bytes=...)`;
el valor de la ruta tiene prioridad sobre el del servidor.

## Handshake

La funcion `handshake_websocket_with_options()` valida:

- `Sec-WebSocket-Key`
- `Connection: Upgrade`
- `Upgrade: websocket`
- `Sec-WebSocket-Version: 13`

Tambien negocia subprotocolos compatibles cuando se declaran en `supported_subprotocols`.

### Origen permitido (`Origin`)

Los navegadores envian `Origin` en cada handshake y adjuntan las cookies del
usuario sin importar que pagina abrio el socket. Sin comprobarlo, cualquier web
podria usar un canal autenticado por cookies (*Cross-Site WebSocket Hijacking*).
Por eso el servidor aplica esta politica antes del `101 Switching Protocols`:

- Por defecto se aceptan las peticiones sin `Origin` (clientes que no son un
  navegador) y las del mismo origen, cuyo `Origin` coincide con `Host`.
- Cualquier otro origen recibe `403`.

```python
# Para toda la app
app = App(ws_allowed_origins=["https://app.example", "https://admin.example"])

# Por ruta: una lista, "*" para cualquiera, o una funcion propia
@app.ws("/public", allowed_origins="*")
def public(ws, request): ...

@app.ws("/partners", check_origin=lambda origin, request: origin.endswith(".partner.example"))
def partners(ws, request): ...
```

La misma regla se aplica a WebSocket sobre HTTP/3 (RFC 9220). La logica esta en
`wsbuilder.ws.websocket_origin_allowed()`.

### Capacidad

Un WebSocket abierto ya no ocupa un hilo de trabajo HTTP: al completar el
handshake devuelve su hueco y pasa a contar contra
`HTTPServer(max_ws_connections=1024)`. Los handshakes por encima de ese limite
reciben `503` con `Retry-After`. Configura `idle_timeout` o
`keepalive_interval`/`pong_timeout` en las rutas para liberar conexiones
muertas.

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

Un timeout de lectura a mitad de un frame no pierde los bytes ya recibidos: se
conservan y el frame se completa en la siguiente lectura, asi que el framing no
se desincroniza. Mientras sigan llegando bytes de un frame, la conexion no
cuenta como inactiva; `idle_timeout` solo vence si el par deja de enviar.

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
