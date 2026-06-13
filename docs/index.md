<div class="hero">

# wsbuilder

Libreria Python para construir servidores HTTP, WebSocket y utilidades de infraestructura con una API pequena y composable.

</div>

## Mapa de plataforma

<div class="diagram">
<div class="diagram-title">Mapa de plataforma</div>
<div class="diagram-track">
<div class="diagram-node">Cliente</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">HTTPServer</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">App</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">Router</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">HTTP / WS</div>
</div>
<div class="diagram-rows" style="margin-top: 1rem;">
<div class="diagram-row">
<div class="diagram-step">Security / Cache / Metrics / Tasks</div>
<div class="diagram-arrow">→</div>
<div class="diagram-step">ORM / Headers / DNS</div>
<div class="diagram-note">Capas auxiliares que acompañan al flujo principal.</div>
</div>
<div class="diagram-row">
<div class="diagram-step">ORM</div>
<div class="diagram-arrow">→</div>
<div class="diagram-step">SQLite</div>
<div class="diagram-note">Persistencia local y determinista.</div>
</div>
</div>
</div>

## Bloques principales

<div class="cards">
<div class="card"><strong>HTTP</strong>Router, request/response, stream chunked y parseo de query string.</div>
<div class="card"><strong>WebSocket</strong>Handshake, frames, control de ping/pong y errores de protocolo.</div>
<div class="card"><strong>Persistencia</strong>Modelos SQLite, `QuerySet`, transacciones y replicas de lectura.</div>
<div class="card"><strong>Infra</strong>Cache, seguridad, metricas, proxy vhost, balanceo, tareas, DNS local y utilidades de protocolo.</div>
</div>

## Flujo mental

<div class="diagram">
<div class="diagram-title">Flujo mental</div>
<div class="diagram-stack">
<div class="diagram-row">
<div class="diagram-step">Cliente</div>
<div class="diagram-arrow">→</div>
<div class="diagram-step">HTTPServer</div>
<div class="diagram-note">Recibe la peticion.</div>
</div>
<div class="diagram-row">
<div class="diagram-step">HTTPServer</div>
<div class="diagram-arrow">→</div>
<div class="diagram-step">App.dispatch</div>
<div class="diagram-note">Convierte bytes en request utilizable.</div>
</div>
<div class="diagram-row">
<div class="diagram-step">App.dispatch</div>
<div class="diagram-arrow">→</div>
<div class="diagram-step">Handler</div>
<div class="diagram-note">Ejecuta el negocio y produce datos.</div>
</div>
<div class="diagram-row">
<div class="diagram-step">Handler</div>
<div class="diagram-arrow">→</div>
<div class="diagram-step">Response</div>
<div class="diagram-note">Serializa y responde.</div>
</div>
</div>
</div>

## Casos de uso principales

<div class="cards">
<div class="card"><strong>APIs pequenas</strong>Construye endpoints JSON y vistas HTML sin arrastrar un framework grande.</div>
<div class="card"><strong>Tiempo real</strong>Usa WebSocket para chat, eventos y estados en vivo.</div>
<div class="card"><strong>SQLite serio</strong>Modela datos, transacciones y lecturas optimizadas con una capa consistente.</div>
<div class="card"><strong>Control interno</strong>Agrega cache, seguridad, metricas y tareas sin salir de la misma API.</div>
</div>

## Guia de aplicacion

<div class="cards">
<div class="card"><strong>Completa</strong>Una guia en tabs con App, ORM, proxy, DNS, seguridad, metricas y tareas para aprender todo junto en GitHub Pages.</div>
<div class="card"><strong>Facil</strong>Parte de un servicio pequeno, correlo localmente y entiende el flujo basico de HTTP, JSON y HTML.</div>
<div class="card"><strong>Avanzada</strong>Arma una aplicacion real con seguridad, cache, metricas, tareas, WebSocket y SQLite optimizado.</div>
</div>

1. Abre [Guia completa](guia-completa.md) si quieres una vista guiada con todos los modulos y un ejemplo end to end.
2. Abre [Aplicacion - Facil](application/easy.md) si quieres construir tu primer servicio completo sin perderte.
3. Abre [Aplicacion - Avanzada](application/advanced.md) si ya sabes que vas a operar un servicio serio.
4. Vuelve a [Arquitectura](architecture.md) si quieres entender por que cada pieza esta separada asi.

## Por que esta libreria es fuerte

- Tiene fronteras claras entre transporte, negocio y observabilidad.
- Exige poco para empezar y permite crecer por modulos.
- Usa componentes simples que puedes leer, depurar y extender sin magia opaca.
- Expone helpers de bajo nivel para cuando necesitas controlar el protocolo, no solo abstraerlo.
- Encaja bien como base de servicios pequenos o como pieza interna de una arquitectura mayor.

## Vista rapida

```python
from wsbuilder import App, Response, Database, Model, IntegerField, TextField

app = App(cors_allow_origin="*")
app.enable_metrics()

class User(Model):
    id = IntegerField(primary_key=True, auto_increment=True)
    username = TextField(unique=True, index=True, null=False)
    email = TextField(null=False)

db = Database("app.db")
User.create_table(db)

@app.view("/")
def home(_request):
    return Response.html("<h1>wsbuilder</h1>")

@app.api("/api/health")
def health(_request):
    return {"ok": True}

app.run("0.0.0.0", 8765)
```

## Como leer esta documentacion

1. Empieza por [Empezar](getting-started.md) si quieres levantar algo rapido.
2. Sigue con [Aplicacion](application/index.md) para elegir entre la guia completa, la facil o la avanzada.
3. Si quieres estudiar todo el sistema junto, abre [Guia completa](guia-completa.md).
4. Abre [Arquitectura](architecture.md) para entender el flujo interno.
5. Abre [Ayuda](help/index.md) si estas pensando en Microservicios o topologias distribuidas.
6. Usa [Referencia](reference/index.md) para ir directo a una clase, modulo o helper.

## Contribucion y soporte

- Trabaja en ramas `feat/<nombre>` o `fix/<nombre>` creadas desde `main`.
- Abre PRs hacia `main` con cambios pequenos y faciles de revisar.
- Si detectas una vulnerabilidad, usa el canal privado en vez de una issue publica.
- Soporte opcional por BTC: `bc1q3lhxpr9yantvefmvhpd2h4lu0ykf3t45zvuve2`
