# DNS

`LocalDNSServer` sirve respuestas DNS locales y puede actuar como capa de pruebas, fallback o resolucion interna.

## Arranque

```python
from wsbuilder import LocalDNSServer

dns = LocalDNSServer(host="127.0.0.1", port=5533)
dns.start()
```

## Records

Puedes declarar records con diccionarios o listas:

```python
records = {
    "example.local": {
        "A": "10.10.0.10",
        "AAAA": "::1",
        "TXT": "hello",
    }
}
```

Tambien soporta `MX`, `SRV`, `CNAME`, `PTR` y registros crudos con `hex`.

## Helpers

- `add_record(name, rtype, value, ...)`
- `add_raw_record(name, rtype, rdata, ...)`
- `remove_record(name, ...)`
- `clear_records(keep_defaults=True)`

## Resolucion

La resolucion soporta:

- wildcard records
- cadenas `CNAME`
- upstream servers opcionales
- fallback cuando el upstream no responde

## Ejemplo de test

```python
dns = LocalDNSServer(
    host="127.0.0.1",
    port=0,
    records={"example.local": {"A": "10.10.0.10"}},
    ttl=120,
)
```

## Cuando usarlo

- tests de resolucion local
- microservicios internos con nombres estables
- entornos de desarrollo donde no quieres depender de DNS externo

## Nota

`LocalDNSServer` trabaja con UDP y tiene un parser y encoder propios para los tipos mas utiles.
