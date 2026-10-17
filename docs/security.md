# Seguridad

`SecurityPolicy` agrega control de acceso, listas blancas/negras, rate limiting y bloqueos temporales.

## Instalacion

```python
from wsbuilder import App, SecurityPolicy, install_security

app = App()
policy = install_security(app, SecurityPolicy(rate_limit_requests=240))
```

## Piezas principales

- `ACLRule` para reglas por metodo, ruta, IP o headers.
- `SecurityDecision` para representar la decision final.
- `SecurityPolicy` para evaluar requests y registrar bloqueos.

## Reglas comunes

```python
policy.add_whitelist("127.0.0.1")
policy.add_blacklist("10.0.0.0/8")
policy.deny(name="admin-post", methods=("POST",), path="/admin")
policy.allow(name="health", path="/api/health", methods=("GET",))
```

## Rate limiting

```python
policy = SecurityPolicy(
    rate_limit_requests=10,
    rate_limit_window_seconds=60.0,
    block_duration_seconds=300.0,
)
```

Cuando un cliente supera el limite, la policy devuelve una respuesta con `429` y `Retry-After`.

## Observacion de respuestas

`observe_response(request, status_code)` se puede usar para alimentar heuristicas internas.
La libreria puede elevar bloqueos temporales cuando detecta patrones sospechosos repetidos.

## Snapshot

```python
state = policy.snapshot()
```

Ese snapshot incluye:

- contadores de allow/deny/block
- listas activas
- reglas ACL
- bloques temporales

## Ejemplo de ruta protegida

```python
from wsbuilder import App, SecurityPolicy

app = App()
policy = app.enable_security()
policy.deny(name="admin", path="/api/admin", methods=("POST",))

@app.api("/api/admin", methods=("POST",))
def admin(_request):
    return {"ok": True}
```

## Recomendacion

- Usa whitelist para entornos internos.
- Usa blacklist para IPs conocidas.
- Ajusta `trust_x_forwarded_for` solo si realmente estas detras de un proxy confiable.
- Mantener `acl_default="allow"` simplifica APIs publicas; `deny` es mejor para superficies cerradas.
