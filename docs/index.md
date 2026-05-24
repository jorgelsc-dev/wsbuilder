<div class="hero">

# wsbuilder

Framework ligero de Python para servidores HTTP, WebSocket y utilidades de infraestructura sin depender de frameworks pesados.

</div>

## Lo que incluye

<div class="cards">
<div class="card"><strong>HTTP</strong>Router con `view()` y `api()`, respuestas texto, HTML, JSON y stream.</div>
<div class="card"><strong>WebSocket</strong>Handshake, frames, subprotocolos y callbacks de ciclo de vida.</div>
<div class="card"><strong>ORM</strong>Modelos SQLite, `QuerySet`, transacciones y helpers SQL.</div>
<div class="card"><strong>Infra</strong>Cache, seguridad, metricas, DNS local y replicas SQLite.</div>
</div>

## Atajos

- [Empezar](getting-started.md)
- [Arquitectura](architecture.md)
- [Referencia completa](reference/index.md)
- [GitHub Pages](deployment/github-pages.md)

## Vista rapida

```python
from wsbuilder import App, Response

app = App(cors_allow_origin="*")
app.enable_metrics()

@app.view("/")
def home(_request):
    return Response.html("<h1>wsbuilder</h1>")

@app.api("/api/health")
def health(_request):
    return {"ok": True}

app.run("0.0.0.0", 8765)
```

## Como leer esta documentacion

1. Empieza por [Empezar](getting-started.md) si solo quieres ejecutar algo rapido.
2. Sigue con [Arquitectura](architecture.md) para entender el flujo interno.
3. Usa [Referencia completa](reference/index.md) cuando necesites clases, funciones o integraciones concretas.
