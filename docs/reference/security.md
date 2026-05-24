# Seguridad

El motor de seguridad se centra en tres capas:

1. ACL por ruta, metodo, IP y headers.
2. Lista blanca / lista negra.
3. Bloqueos temporales por rate limiting o comportamiento sospechoso.

## Clases y funciones

- `SecurityPolicy`
- `ACLRule`
- `SecurityDecision`
- `install_security(app, policy=None)`

## Ejemplo

```python
from wsbuilder import App, SecurityPolicy, install_security

app = App()
policy = SecurityPolicy(rate_limit_requests=120, rate_limit_window_seconds=60)
install_security(app, policy)
```

## Capacidades

- ACL por `path`, `path_prefix`, `path_regex`.
- Filtros por `methods`.
- Restriccion por `ip_cidrs`.
- Requerir o prohibir TLS por ruta.
- Reglas por headers exactos o regex.
- Bloqueo temporal con `Retry-After` cuando corresponde.

