# DNS local

<div class="diagram">
<div class="diagram-title">DNS local</div>
<div class="diagram-track">
<div class="diagram-node">Client</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">LocalDNSServer</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">localhost</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">Custom / NXDOMAIN</div>
</div>
<div class="diagram-note" style="margin-top: 0.85rem;">Pensado para laboratorio, desarrollo y demos sin infraestructura externa.</div>
</div>

`LocalDNSServer` expone un servidor DNS UDP minimo pensado para entornos locales.

## Valores por defecto

- `host="127.0.0.1"`
- `port=5533`
- `ttl=60`

## Ejemplo

```python
from wsbuilder import LocalDNSServer

dns = LocalDNSServer()
dns.start()
```

## Comportamiento

- Responde `A` y `AAAA` para `localhost`.
- Acepta registros personalizados.
- Devuelve `NXDOMAIN` para nombres desconocidos.

## Casos de uso

- Resolver nombres locales en desarrollo.
- Simular dominios de prueba sin infraestructura externa.
- Montar un resolver pequeno para demos o laboratorios.

## Rol del modulo

- Cerrar el circuito de una demo local sin depender de DNS externo.
- Ayudar en pruebas de integracion y laboratorios.
- Mantener una pieza de infraestructura simple y portable.
