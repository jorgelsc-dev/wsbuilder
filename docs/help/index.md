# Ayuda

Esta seccion agrupa guias practicas para usar `wsbuilder` en escenarios reales, con foco en decisiones de arquitectura y patrones de uso.

## Que encontraras aqui

<div class="cards">
<div class="card"><strong>Microservicios</strong>Como dividir responsabilidades, aislar datos y exponer salud, metricas y contratos.</div>
<div class="card"><strong>Operacion</strong>Recomendaciones para cache, seguridad, tareas y observabilidad por servicio.</div>
<div class="card"><strong>Integracion</strong>Como conectar servicios entre si sin mezclar persistencia ni estado local.</div>
</div>

## Mapa de ayuda

```mermaid
flowchart LR
    A[Idea de arquitectura] --> B[Definir servicios]
    B --> C[Separar datos]
    C --> D[Exponer contrato HTTP]
    D --> E[Agregar seguridad y metricas]
    E --> F[Operar y escalar]
    F --> G[Revisar puntos de fallo]
```

## Rutas recomendadas

1. Empieza por [Microservicios](microservices.md) si estas diseñando una plataforma distribuida.
2. Lee [Arquitectura](../architecture.md) para entender como se conecta cada modulo interno.
3. Consulta [Referencia](../reference/index.md) cuando necesites la API exacta de una clase o helper.

## Criterio rapido

- Usa `App` como limite del servicio.
- Usa `Database` por servicio, no una base compartida para todos.
- Usa `TaskManager` para trabajo local, no para coordinar procesos distribuidos.
- Usa `AppMetrics` y `SecurityPolicy` en cada servicio expuesto.
