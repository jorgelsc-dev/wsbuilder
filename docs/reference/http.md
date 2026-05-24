# HTTP y respuesta

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

## Parser y writer

- `parse_http_request(conn, max_header_bytes=65536)` lee y valida la request.
- `send_http_response(conn, response)` serializa la respuesta.

Comportamientos importantes:

- Si la respuesta es stream, se usa `Transfer-Encoding: chunked` cuando no existe `Content-Length`.
- Si no indicas `Connection`, el servidor cierra la conexion al final.
- Las cabeceras grandes generan `431` y los cuerpos muy grandes `413`.

