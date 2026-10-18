# HTTP

La capa HTTP cubre requests, responses, headers, cookies y serializacion basica.

## `Request`

`Request` representa una peticion ya parseada:

- `method`
- `path`
- `query_string`
- `query`
- `headers`
- `body`
- `client`
- `tls`

Helpers utiles:

```python
body_text = request.text()
payload = request.json()
```

## `Response`

`Response` soporta cuatro formas comunes:

```python
Response.text("ok")
Response.html("<h1>hola</h1>")
Response.json({"ok": True})
Response.stream(iter(["a", "b", "c"]), content_type="text/plain")
```

`Response.stream` devuelve una respuesta en streaming con soporte para generadores, iterables y objetos con `read()`.
Al terminar el envio (o si el cliente se desconecta) el servidor llama a
`close()` sobre el origen del stream cuando existe.

`Response.headers` es un diccionario con un valor por nombre. Un valor puede ser
una lista o tupla para enviar el campo repetido, que es lo que necesita
`Set-Cookie`:

```python
Response.text("ok", headers={"Set-Cookie": ["a=1; Path=/", "b=2; HttpOnly"]})
```

HTTP/1 escribe una linea por valor y HTTP/2 y HTTP/3 un campo por valor. En las
vistas con workers, la cookie de afinidad se anade a las del handler, de modo
que `response.headers["Set-Cookie"]` puede ser una lista.

## Enrutado

`App.view()` y `App.api()` usan el router interno:

```python
from wsbuilder import App, Response

app = App(cors_allow_origin="*")

@app.view("/hello")
def hello(_request):
    return Response.text("hola")

@app.api("/api/health")
def health(_request):
    return {"ok": True}
```

## CORS y OPTIONS

`App.dispatch()` responde `OPTIONS` automaticamente cuando detecta una ruta registrada.
Si `cors_allow_origin` esta definido, la respuesta incluye:

- `Access-Control-Allow-Origin`
- `Access-Control-Allow-Methods`
- `Access-Control-Allow-Headers`
- `Vary: Origin` cuando no se usa `*`

## Headers y cookies

La libreria trae helpers para nombres, lectura y escritura:

```python
from wsbuilder import (
    add_header,
    build_set_cookie,
    get_cookie,
    get_header,
    has_header,
    normalize_header_name,
    set_header,
)
```

`add_header(headers, name, value)` anade un valor sin reemplazar los
existentes: el campo pasa a ser una lista. `get_header` devuelve el valor tal
cual, que puede ser una lista si el campo se repite.

Usos comunes:

- leer `Authorization`, `Content-Type` o cualquier header normalizado.
- construir `Set-Cookie` con `HttpOnly`, `Secure`, `SameSite` y `Max-Age`.
- inyectar headers de respuesta sin reescribir diccionarios manualmente.

## Servidor HTTP

`HTTPServer` conecta el socket con `App.dispatch()`. Si quieres ejecutar tu app dentro de otro proceso,
usa `app.run(host, port)` o integra el server en tu propio bootstrap.

## Ejemplo compacto

```python
from wsbuilder import App, Response

app = App()

@app.view("/sum")
def sum_view(request):
    a = int(request.query.get("a", "0"))
    b = int(request.query.get("b", "0"))
    return Response.json({"result": a + b})
```

## Puntos de extension

- `parse_http_request` para parseo de requests en bruto.
- `send_http_response` si necesitas escribir respuestas manualmente.
- `parse_query_string` para utilidades ligeras de query strings.
