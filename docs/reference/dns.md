# DNS local / personal

<div class="diagram">
<div class="diagram-title">DNS local / personal</div>
<div class="diagram-track">
<div class="diagram-node">Cliente</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">LocalDNSServer</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">Zona local</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">Fallback upstream (opcional)</div>
</div>
<div class="diagram-note" style="margin-top: 0.85rem;">Sirve como DNS autoritativo local y tambien como DNS personal con reenvio a upstream.</div>
</div>

`LocalDNSServer` es un servidor DNS UDP autoritativo para zonas locales con soporte de registros generales y fallback opcional a resolvers upstream.

## Valores por defecto

- `host="127.0.0.1"`
- `port=5533`
- `ttl=60`
- `upstream_servers=None`
- `upstream_timeout=1.2`
- `fallback_to_upstream`: se activa automaticamente si defines `upstream_servers`.

## Inicio rapido

```python
from wsbuilder import LocalDNSServer

dns = LocalDNSServer(
    host="0.0.0.0",
    port=53,  # usa 53 para que clientes no tengan que especificar puerto
    records={
        "home.local": {"A": "192.168.1.10", "AAAA": "fd00::10"},
        "api.home.local": {"CNAME": "home.local"},
    },
    upstream_servers=["1.1.1.1:53", "8.8.8.8:53"],
)
dns.start()
```

## Parametros del servidor

- `host`: IP de escucha (`127.0.0.1`, `0.0.0.0`, `::`, etc).
- `port`: puerto UDP de escucha.
- `records`: zona local (dict o lista de registros).
- `ttl`: TTL por defecto para todos los registros sin TTL explicito.
- `upstream_servers`: lista de DNS externos para fallback.
  - Formatos validos:
    - `"1.1.1.1"`
    - `"1.1.1.1:53"`
    - `"[2606:4700:4700::1111]:53"`
    - `{"host": "8.8.8.8", "port": 53}`
    - `("9.9.9.9", 53)`
- `upstream_timeout`: timeout de cada consulta upstream.
- `fallback_to_upstream`: si `True`, las consultas sin respuesta local se reenvian.

## API en runtime

- `start()`: inicia el hilo del servidor.
- `serve_forever()`: loop bloqueante.
- `close()`: cierra socket e hilo.
- `add_record(name, rtype, value, ttl=None, rclass="IN")`
- `add_raw_record(name, rtype, rdata, ttl=None, rclass="IN")`
- `remove_record(name, rtype=None, value=None, rclass=None)`
- `clear_records(keep_defaults=True)`

Siempre se instalan por defecto:

- `localhost A 127.0.0.1`
- `localhost AAAA ::1`

## Formatos de `records`

### 1) Formato legacy (IP directa)

```python
records = {
    "app.local": "192.168.1.50",
    "db.local": ["192.168.1.60", "fd00::60"],
}
```

### 2) Formato por nombre y tipo

```python
records = {
    "example.local": {
        "ttl": 300,
        "A": ["10.0.0.10", "10.0.0.11"],
        "AAAA": "fd00::10",
        "TXT": ["v=spf1 -all", "build=2026-05-27"],
        "MX": {"preference": 10, "exchange": "mail.example.local"},
        "SRV": {"priority": 1, "weight": 10, "port": 443, "target": "edge.example.local"},
        "CAA": {"flags": 0, "tag": "issue", "value": "letsencrypt.org"},
    }
}
```

### 3) Lista plana de registros

```python
records = [
    {"name": "api.local", "type": "A", "value": "10.0.2.20", "ttl": 120},
    {"name": "api.local", "type": "TXT", "value": "env=dev"},
    {"name": "raw.local", "type": "TYPE65280", "hex": "a1b2c3d4"},
]
```

## Tipos de registros soportados

El modulo acepta tipos por:

- nombre (`"A"`, `"MX"`, `"CAA"`, etc),
- numero (`1`, `15`, `257`, etc),
- sintaxis `"TYPE####"` para tipos no mapeados (`"TYPE65280"`).

### Tipos con codificador nativo

- `A` (`"192.168.1.10"`)
- `AAAA` (`"fd00::10"`)
- `TXT` (`"texto"` o lista de strings)
- `NS`, `CNAME`, `PTR`, `MB`, `MD`, `MF`, `MG`, `MR`, `DNAME` (nombre DNS)
- `MX` (`{"preference": 10, "exchange": "mail.local"}` o `(10, "mail.local")`)
- `SRV` (`{"priority": 1, "weight": 5, "port": 443, "target": "svc.local"}`)
- `SOA` (`{"mname": "...", "rname": "...", "serial": ..., "refresh": ..., "retry": ..., "expire": ..., "minimum": ...}`)
- `CAA` (`{"flags": 0, "tag": "issue", "value": "letsencrypt.org"}`)
- `NAPTR` (`{"order": 10, "preference": 100, "flags": "s", "services": "E2U+sip", "regexp": "", "replacement": "."}`)
- `URI` (`{"priority": 1, "weight": 1, "target": "https://example.local/api"}`)
- `DNSKEY`, `DS`, `TLSA`, `SSHFP` (con campos estructurados).

### Tipos generales (raw)

Para cualquier tipo no codificado de forma nativa, puedes pasar `rdata` raw:

```python
records = [
    {"name": "bin.local", "type": "TYPE65001", "rdata": b"\x01\x02\x03"},
    {"name": "bin.local", "type": 65002, "hex": "deadbeef"},
    {"name": "bin.local", "type": 65003, "base64": "AQIDBA=="},
]
```

## Clases DNS soportadas

- `IN`, `CH`, `HS`, `NONE`, `ANY`.
- tambien por codigo numerico (`1`, `3`, `4`, `254`, `255`, etc).

## Comportamiento de resolucion

- soporte de consultas `ANY`.
- soporte de wildcard: `*.dominio.local`.
- cadena CNAME basica (resuelve CNAME y luego intenta responder el tipo consultado).
- `NXDOMAIN` si el nombre no existe localmente y no hay fallback.
- con fallback activado:
  - reenvia consultas no resueltas a upstream;
  - si upstream falla, responde `SERVFAIL`.

## Como usarlo como DNS personal

### Paso 1: ejecutar en puerto 53

El cliente DNS normal usa puerto 53. Debes levantar el servidor en `port=53`.

```python
dns = LocalDNSServer(host="0.0.0.0", port=53, records=..., upstream_servers=["1.1.1.1:53"])
dns.serve_forever()
```

### Paso 2: abrir firewall UDP/53

Permite trafico UDP 53 desde tus clientes (LAN o localhost segun tu caso).

### Paso 3: configurar el cliente para usar tu DNS

#### Linux (NetworkManager)

En la conexion de red, define DNS manual:

- GUI: Ajustes de red -> IPv4/IPv6 -> DNS -> IP del servidor DNS.
- CLI (`nmcli`):

```bash
nmcli connection modify "<conexion>" ipv4.dns "192.168.1.10" ipv4.ignore-auto-dns yes
nmcli connection up "<conexion>"
```

#### macOS

- System Settings -> Network -> (interfaz) -> Details -> DNS -> agrega la IP del DNS local.

#### Windows 10/11

- Settings -> Network & Internet -> Adapter options -> propiedades del adaptador -> IPv4/IPv6 -> DNS manual -> IP de tu servidor.

#### Android

- Wi-Fi -> red actual -> IP settings/Advanced -> DNS 1 = IP del servidor.

#### iOS/iPadOS

- Wi-Fi -> (i) -> Configure DNS -> Manual -> add server.

### Paso 4: validar

Desde un cliente:

```bash
dig @192.168.1.10 example.local A
dig @192.168.1.10 example.local MX
dig @192.168.1.10 google.com A
```

- la primera y segunda deben salir de tu zona local;
- la tercera debe salir por fallback upstream (si esta activado).

## Ejemplo integral

```python
from wsbuilder import LocalDNSServer

records = {
    "example.local": {
        "ttl": 120,
        "A": ["192.168.1.20", "192.168.1.21"],
        "AAAA": "fd00::20",
        "TXT": ["site=example", "env=lab"],
        "MX": {"preference": 10, "exchange": "mail.example.local"},
        "SRV": {"priority": 1, "weight": 5, "port": 443, "target": "edge.example.local"},
        "SOA": {
            "mname": "ns1.example.local",
            "rname": "admin.example.local",
            "serial": 2026052701,
            "refresh": 3600,
            "retry": 600,
            "expire": 1209600,
            "minimum": 60,
        },
        "CAA": {"flags": 0, "tag": "issue", "value": "letsencrypt.org"},
    },
    "*.dev.local": {"A": "192.168.1.30"},
    "api.dev.local": {"CNAME": "example.local"},
}

dns = LocalDNSServer(
    host="0.0.0.0",
    port=53,
    ttl=60,
    records=records,
    upstream_servers=["1.1.1.1:53", "8.8.8.8:53"],
    fallback_to_upstream=True,
)
dns.serve_forever()
```
