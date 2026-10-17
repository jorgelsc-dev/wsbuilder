# mDNS y descubrimiento de servicios

`LocalMDNSServer` publica servicios en la red local mediante mDNS y DNS-SD.
Los clientes pueden descubrir el puerto, el hostname `.local`, las direcciones
IPv4/IPv6 y los metadatos TXT sin configurar un servidor DNS central.

## Instalacion

El soporte usa `python-zeroconf` como dependencia opcional:

```bash
python -m pip install 'wsbuilder[mdns]'
```

El paquete base sigue sin dependencias de runtime. Importar `wsbuilder` o las
APIs mDNS no abre sockets ni requiere `zeroconf`; la dependencia se necesita al
iniciar, publicar o descubrir servicios.

## Publicar una app HTTP

```python
from wsbuilder import App, LocalMDNSServer

app = App()

@app.api("/api/health")
def health(_request):
    return {"ok": True}

with LocalMDNSServer(interfaces=["192.168.1.10"]) as mdns:
    service = mdns.register_service(
        "Mi API",
        port=8765,
        addresses=["192.168.1.10"],
        hostname="mi-api.local",
        properties={"path": "/", "health": "/api/health"},
    )
    app.run("0.0.0.0", 8765)
```

Cambia `192.168.1.10` por la IP local de la maquina. `addresses` contiene las
direcciones que los clientes usaran para conectarse; `0.0.0.0` y `::` sirven para
escuchar, pero no son direcciones que puedas anunciar.

El tipo predeterminado es `_http._tcp.local.`. Puedes usar otros tipos como
`_https._tcp` o `_mi-api._tcp.local.`; el sufijo `.local.` se agrega a los tipos
cortos. El hostname tambien acepta una forma corta como `mi-api`.

Al registrar se publican records PTR, SRV, TXT y A/AAAA. El backend comprueba
conflictos de nombres de instancia y maneja los anuncios y las respuestas mDNS.
Si otra instancia ya usa el nombre, se propaga
`zeroconf.NonUniqueNameException`. Con `allow_name_change=True`, el backend elige
otro nombre; `service.name` contiene el nombre completo que se registro.

## Descubrir y resolver

```python
from wsbuilder import LocalMDNSServer, discover_mdns_services

services = discover_mdns_services("_http._tcp", timeout=3.0)
for service in services:
    print(service.name, service.hostname, service.port, service.addresses)
    print(service.properties.get(b"path"))

with LocalMDNSServer() as mdns:
    service = mdns.resolve_service("Mi API", timeout=2.0)
    if service is not None:
        print(service.hostname, service.port)
```

`discover_mdns_services` devuelve una tupla de snapshots `MDNSService` de las
instancias que logro resolver durante la ventana de busqueda. Devuelve `()` si
no encuentra ninguna. Los servicios retirados durante la busqueda se descartan.
`resolve_service` acepta un nombre de instancia corto o completo y devuelve
`None` si vence el tiempo. Todos los timeouts se expresan en segundos; son APIs
sincronas que bloquean al llamador durante la busqueda.

Cada snapshot incluye `name`, `service_type`, `hostname`, `port`, `addresses`,
`properties` e `interface_index`. Los TXT descubiertos conservan claves y valores
como bytes; un valor `None` representa un atributo sin valor. Los snapshots
devueltos son inmutables. Al publicar, puedes pasar claves ASCII y valores
`str`, `bytes` o `None`.

## Interfaces e IPv6

Por defecto se utiliza multicast IPv4 en todas las interfaces disponibles.
`interfaces` permite elegir IPs locales de interfaces, y `ip_version` acepta
`"v4"`, `"v6"` o `"all"` para seleccionar el transporte multicast. Por ejemplo:

```python
mdns = LocalMDNSServer(ip_version="all")
```

Puedes publicar records A y AAAA en un mismo servicio independientemente del
transporte multicast. Las direcciones IPv6 link-local descubiertas conservan el
scope de interfaz, como `fe80::1%3`, cuando el backend lo conoce. La seleccion
avanzada de interfaces IPv6 sigue las opciones de
[python-zeroconf](https://python-zeroconf.readthedocs.io/en/latest/api.html).

## Ciclo de vida

- `start()` abre el backend una sola vez y devuelve el servidor.
- `register_service(...)` inicia el backend si hace falta y devuelve un snapshot.
- `services` muestra los servicios publicados por esa instancia.
- `unregister_service(service)` acepta un snapshot o un nombre y envia goodbyes.
- `close()` retira todos los servicios y libera sockets y threads. Es idempotente.
- El context manager cierra los recursos incluso si el codigo dentro falla.

Tambien puedes usar `try/finally` y llamar a `close()` al terminar. Cerrar una
instancia permite iniciarla de nuevo; los anuncios anteriores no se restauran.
El descubrimiento cierra automaticamente su browser y backend temporal.

## Alcance

mDNS opera en el enlace local, usando UDP 5353; la red debe permitir multicast.
La resolucion de `.local` desde un navegador depende del soporte mDNS del sistema
operativo. La libreria no modifica su configuracion DNS.

`LocalDNSServer` sigue siendo el servidor DNS unicast para records arbitrarios,
wildcards y upstreams. `LocalMDNSServer` publica y descubre servicios DNS-SD y sus
hosts; no incorpora fallback a DNS externo.

Referencias: [RFC 6762 (mDNS)](https://www.rfc-editor.org/rfc/rfc6762) y
[RFC 6763 (DNS-SD)](https://www.rfc-editor.org/rfc/rfc6763).
