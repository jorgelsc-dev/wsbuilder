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

## Guia de aplicacion

<div class="cards">
<div class="card"><strong>Completa</strong><br>Ruta: <code>docs/guia-completa.md</code><br><a href="guia-completa.md">Abrir guia</a></div>
<div class="card"><strong>Facil</strong><br>Ruta: <code>docs/application/easy.md</code><br><a href="application/easy.md">Abrir guia</a></div>
<div class="card"><strong>Avanzada</strong><br>Ruta: <code>docs/application/advanced.md</code><br><a href="application/advanced.md">Abrir guia</a></div>
</div>

<div class="diagram">
<div class="diagram-title">Ruta de lectura</div>
<div class="diagram-track">
<div class="diagram-node">Inicio</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">Aplicacion</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">Arquitectura</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">Referencia</div>
</div>
</div>
