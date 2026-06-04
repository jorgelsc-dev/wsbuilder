# Aplicacion

Esta seccion agrupa dos rutas de trabajo:

- una guia facil para construir una app funcional sin perder el hilo;
- una guia avanzada para operar un servicio real con seguridad, observabilidad y optimizacion.

## Como elegir

<div class="cards">
<div class="card"><strong>Facil</strong>Si estas empezando, quieres una API pequena o necesitas una plantilla simple para copiar y adaptar.</div>
<div class="card"><strong>Avanzada</strong>Si ya tienes varios modulos, carga concurrente o requisitos de operacion y rendimiento.</div>
</div>

## Que cubren

<div class="diagram">
<div class="diagram-title">Mapa de aplicacion</div>
<div class="diagram-track">
<div class="diagram-node">Instalar</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">Crear app</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">Agregar rutas</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">Activar capas</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">Operar</div>
</div>
<div class="diagram-note" style="margin-top: 0.85rem;">La guia facil recorre este flujo con una sola app. La avanzada lo desglosa por capas y decisiones de arquitectura.</div>
</div>

## Rutas

1. Abre [Facil](easy.md) si quieres una app completa en pocos pasos.
2. Abre [Avanzada](advanced.md) si necesitas control fino sobre el runtime.
3. Si quieres entender el por que del flujo interno, vuelve a [Arquitectura](../architecture.md).

## Regla practica

- Si solo necesitas responder requests y validar el flujo, usa la guia facil.
- Si vas a mezclar cache, seguridad, tareas, metrics, replicas y WebSocket, usa la avanzada.
- Si vas a exponer el proyecto a otros equipos, trata la guia avanzada como tu baseline operativa.
