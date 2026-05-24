# HTTP y respuesta

<div class="diagram">
<div class="diagram-title">HTTP y respuesta</div>
<div class="diagram-track">
<div class="diagram-node">Socket</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">parse_http_request</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">Request</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">App.dispatch</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">Response</div>
</div>
<div class="diagram-note" style="margin-top: 0.85rem;">La entrada se parsea, pasa por la app y termina como texto, JSON, HTML o stream.</div>
</div>

## Request

`Request` modela la request entrante con:

- `method`
- `path`
- `query_string`
- `query`
- `headers`
- `body`
- `client`
- `tls`

Helpers:

- `request.text()`
- `request.json()`

## Response

`Response` representa la salida del handler.

Constructores utiles:

- `Response.text(...)`
- `Response.json(...)`
- `Response.html(...)`
- `Response.stream(...)`

Ejemplo:

```python
from wsbuilder import Response

def handler(_request):
    return Response.json({"ok": True})
```

## Flujo HTTP

1. El servidor lee y valida la peticion.
2. `App.dispatch()` resuelve la ruta.
3. El handler devuelve `str`, `dict`, `list` o `Response`.
4. `Response` se serializa a texto, JSON, HTML o stream chunked.

## Rol del modulo

- Representa la entrada y salida del servicio.
- Normaliza query string, headers y cuerpo.
- Hace que el handler pueda devolver datos simples o un `Response` completo.

## Parser y writer

- `parse_http_request(conn, max_header_bytes=65536)` lee y valida la request.
- `send_http_response(conn, response)` serializa la respuesta.

Comportamientos importantes:

- Si la respuesta es stream, se usa `Transfer-Encoding: chunked` cuando no existe `Content-Length`.
- Si no indicas `Connection`, el servidor cierra la conexion al final.
- Las cabeceras grandes generan `431` y los cuerpos muy grandes `413`.

## Casos de uso

- APIs JSON sencillas con serializacion implicita.
- Paginacion y endpoints de estado con `Response.text()`.
- HTML renderizado directamente desde handlers.
- Flujos de salida incremental para logs, progreso o exportaciones.
