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

## Endurecimiento UDP

- Una consulta con un numero de preguntas distinto de uno (`MAX_QUESTIONS = 1`)
  recibe `FORMERR`.
- Las respuestas se limitan a 512 bytes, o al tamano que anuncia un registro
  EDNS(0) `OPT` del cliente con tope en `max_udp_payload` (1232 por defecto). Si
  no caben, se envia solo la pregunta con el bit `TC`. El servidor no tiene
  listener TCP, asi que un cliente sin EDNS no obtiene respuestas mayores de 512
  bytes; esto evita la amplificacion.
- Un `OSError` al recibir o enviar un datagrama se registra con
  `wsbuilder.logs` y el bucle sigue.
- El reenvio a upstreams corre en un pool acotado (`upstream_workers=4`,
  `max_pending_upstream=64`) y no bloquea las respuestas locales. Con el pool
  lleno se responde `SERVFAIL`; `close()` lo detiene.

Opciones nuevas solo por clave: `upstream_workers`, `max_pending_upstream`,
`max_udp_payload` y `app` (destino de los logs, via `get_log(app)`).

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

Para publicar y descubrir servicios con nombres `.local` mediante multicast,
usa [`LocalMDNSServer` y el soporte mDNS](mdns.md).
