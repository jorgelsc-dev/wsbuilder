# Arquitectura

WSBuilder se organiza alrededor de una idea simple: una aplicacion central que enruta requests y va
activando componentes transversales cuando hacen falta.

## Flujo de request

1. `HTTPServer` recibe la conexion.
2. `parse_http_request` crea un `Request`.
3. `App.dispatch` aplica seguridad, cache y resolucion de ruta.
4. El handler devuelve `Response`, `dict`, `list`, texto o `None`.
5. `App` ajusta CORS, cookies de afinidad, metricas y logs.

## Tipos de ruta

### `view`

Rutas HTML o texto. Pueden usar un pool de workers por ruta si configuras `min_threads` y `max_threads`.

```python
@app.view("/dashboard", min_threads=1, max_threads=4, requests_per_thread=32)
def dashboard(_request):
    return "ok"
```

### `api`

Rutas JSON para APIs y servicios de control.

```python
@app.api("/api/users")
def users(_request):
    return [{"id": 1, "name": "Alice"}]
```

### `ws`

Rutas WebSocket con handshake y callbacks de ciclo de vida.

```python
@app.ws("/ws/")
def chat(ws, _request):
    while True:
        frame = ws.recv_frame()
        if frame.opcode == 0x8:
            break
        ws.send_text(frame.payload.decode("utf-8", errors="ignore"))
```

## Componentes transversales

<div class="ws-grid ws-grid-compact">
  <article class="ws-card">
    <h3>Seguridad</h3>
    <p>Bloquea, limita y audita requests antes de entrar al handler.</p>
  </article>
  <article class="ws-card">
    <h3>Cache</h3>
    <p>Guarda respuestas de `view` y aplica reglas globales o por ruta.</p>
  </article>
  <article class="ws-card">
    <h3>Metricas</h3>
    <p>Expone snapshots JSON y streaming continuo para observabilidad en vivo.</p>
  </article>
  <article class="ws-card">
    <h3>Tareas</h3>
    <p>Ejecuta trabajo en background con cancelacion y control de concurrencia.</p>
  </article>
</div>

## Threads por ruta

Las rutas `view` pueden correr en directo o en un pool controlado por la propia ruta.

- `thread_count=0` usa ejecucion directa.
- `min_threads` y `max_threads` habilitan el pool.
- `requests_per_thread` limita cuanta carga acepta cada worker.
- `affinity_ttl_seconds` y `thread_cookie_name` ayudan a mantener afinidad por cookie.

## Docs nativas

`App.enable_docs()` usa `App.describe()` para construir el JSON de la aplicacion y una pagina HTML simple.
Esto es util cuando quieres introspeccion interna sin montar una documentacion externa.

## Cuando usar cada capa

| Necesidad | Capa |
| --- | --- |
| HTML o texto | `view` |
| JSON o API | `api` |
| Conexion persistente | `ws` |
| Tareas de fondo | `tasks` |
| Control de acceso | `security` |
| Telemetria | `metrics` |
| Respuesta repetida | `cache` |

## Nota practica

La libreria intenta mantenerse en la stdlib. Eso simplifica despliegue, reduce friccion en entornos pequenos y
hace mas obvio el flujo de ejecucion desde socket hasta respuesta.
