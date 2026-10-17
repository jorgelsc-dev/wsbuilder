# WSBuilder Docs

<div class="ws-hero">
  <div class="ws-hero-copy">
    <p class="ws-eyebrow">HTTP + WebSocket + ORM + cache + seguridad + proxy</p>
    <h1>Una base completa para construir servicios Python sin cargar mas de lo necesario.</h1>
    <p class="ws-lead">
      WSBuilder agrupa el servidor, el router, el ORM, el cache, la observabilidad y las utilidades de red en
      un solo paquete. La documentacion esta organizada para que puedas ir de cero a produccion sin perderte.
    </p>
    <div class="ws-actions">
      <a class="ws-button ws-button-primary" href="getting-started/">Empezar</a>
      <a class="ws-button ws-button-secondary" href="reference/">Ver referencia</a>
      <a class="ws-button ws-button-ghost" href="architecture/">Entender arquitectura</a>
    </div>
  </div>

  <aside class="ws-hero-panel" aria-label="Resumen rapido">
    <div class="ws-hero-stat">
      <span>1</span>
      <small>import principal desde `wsbuilder`</small>
    </div>
    <div class="ws-hero-stat">
      <span>4</span>
      <small>capas centrales: HTTP, WebSocket, datos e infraestructura</small>
    </div>
    <div class="ws-hero-stat">
      <span>0</span>
      <small>dependencias runtime obligatorias fuera de la stdlib</small>
    </div>
  </aside>
</div>

## Mapa rapido

<div class="ws-grid">
  <article class="ws-card">
    <p class="ws-kicker">Punto de entrada</p>
    <h2>App</h2>
    <p>Define rutas HTTP, WebSocket, caches, seguridad, metricas, logs y tareas en background.</p>
  </article>
  <article class="ws-card">
    <p class="ws-kicker">Persistencia</p>
    <h2>ORM SQLite</h2>
    <p>Modelos declarativos, filtros, transacciones, replicas de lectura y optimizacion SQLite.</p>
  </article>
  <article class="ws-card">
    <p class="ws-kicker">Observabilidad</p>
    <h2>Metricas y logs</h2>
    <p>Snapshots JSON, streaming en vivo y logs NDJSON para seguimiento simple y automatizable.</p>
  </article>
  <article class="ws-card">
    <p class="ws-kicker">Infraestructura</p>
    <h2>Proxy y DNS</h2>
    <p>Balanceo de trafico, virtual hosts, DNS local y utilidades para escenarios de borde.</p>
  </article>
</div>

## Lo que vas a encontrar

<div class="ws-grid ws-grid-compact">
  <article class="ws-card">
    <h3>Guia de inicio</h3>
    <p>Instalacion, primer servidor, docs en vivo y ejemplos listos para copiar.</p>
    <a href="getting-started/">Abrir guia</a>
  </article>
  <article class="ws-card">
    <h3>Arquitectura</h3>
    <p>Como entra una request, donde se aplica seguridad, cache, metricas y tareas.</p>
    <a href="architecture/">Ver arquitectura</a>
  </article>
  <article class="ws-card">
    <h3>Referencia</h3>
    <p>Lista de modulos y exportaciones publicas, agrupadas por caso de uso.</p>
    <a href="reference/">Abrir referencia</a>
  </article>
  <article class="ws-card">
    <h3>Ayuda</h3>
    <p>Errores comunes, arranque local, validaciones y puntos de diagnostico rapido.</p>
    <a href="help/">Ir a ayuda</a>
  </article>
</div>

## Flujo recomendado

1. Lee la [guia de inicio](getting-started.md) y levanta un `App` minimo.
2. Sigue la [arquitectura](architecture.md) para entender el flujo de request a response.
3. Abre la [referencia](reference/index.md) cuando necesites firmas, parametros o nombres exactos.
4. Usa la [ayuda](help/index.md) para resolver problemas de instalacion, build o despliegue.

## Resumen de uso

| Si quieres... | Lee primero |
| --- | --- |
| Servir HTML o JSON | [HTTP](http.md) |
| Mantener conexiones persistentes | [WebSocket](websocket.md) |
| Guardar datos localmente | [Persistencia](persistence.md) |
| Reducir costo de respuestas | [Cache](cache.md) |
| Controlar acceso y rate limit | [Seguridad](security.md) |
| Medir carga y eventos | [Observabilidad](observability.md) |
| Ejecutar trabajo en segundo plano | [Tareas](tasks.md) |
| Proxy y balanceo | [Proxy](proxy.md) |
| Resolver nombres localmente | [DNS](dns.md) |
| Entrenar modelos sencillos | [IA](ia.md) |

!!! tip "Atajo util"
    Si solo quieres probar la libreria, instala el paquete en editable y arranca el demo:

    ```bash
    python -m pip install -e .
    python -m wsbuilder --host 0.0.0.0 --port 8765
    ```

## Enlaces utiles

- [Repositorio](https://github.com/jorgelsc-dev/wsbuilder)
- [Documentacion en vivo](https://wsbuilder.jorgelsc.dev/)
- [Archivo de ayuda](help/index.md)
- [Referencia completa](reference/index.md)
