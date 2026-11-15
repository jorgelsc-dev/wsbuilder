# Comunicacion entre servicios (RPC)

`wsbuilder.rpc` sirve para que un servicio llame a otro gastando menos red. El
servicio que llama entrega un objeto Python. Ese objeto se codifica directamente
a bytes en formato CBOR (RFC 8949), viaja cifrado y el servicio de destino lo
recibe ya decodificado. En ningun punto del camino se escribe JSON como texto.

```text
dict (servicio A) -> CBOR -> bytes cifrados por HTTP/3 -> CBOR -> dict (servicio B)
```

Si un servicio necesita responder JSON a un navegador, lo genera solo en su
borde. Entre servicios no hace falta.

## Por que ahorra red

- **Cuerpo mas pequeno.** CBOR escribe tipos y longitudes en binario, sin
  comillas, sin escapes y sin numeros como texto. En registros tipicos el cuerpo
  ocupa alrededor de un 30 % menos que el JSON compacto equivalente.
- **Bytes sin base64.** Un `bytes` (audio, imagen, firma) viaja tal cual. En
  JSON tendria que ir en base64, que ocupa un 33 % mas.
- **Una conexion reutilizada.** Con HTTP/3 se mantiene abierta una conexion
  QUIC y cada llamada abre un stream nuevo dentro de ella. Solo la primera
  llamada paga el handshake y una respuesta lenta no bloquea a las demas.
- **Cifrado incluido.** QUIC exige TLS 1.3, asi que todo lo que viaja va
  cifrado. No hace falta una capa de cifrado propia.

!!! note "CPU frente a red"
    El codec CBOR esta escrito en Python puro para no anadir dependencias. El
    modulo `json` de la biblioteca estandar esta escrito en C, de modo que
    codificar y decodificar CBOR consume mas CPU que JSON: unas 3 a 5 veces
    mas, alrededor de 1 ms por cada 20 KB de cuerpo. El ahorro esta en los
    bytes que viajan. Si para un servicio pesa mas la CPU, usa `codec="json"`,
    que mantiene el resto de ventajas (conexion reutilizada, errores
    estructurados, mTLS).

## Servidor

```python
from wsbuilder import App, RpcError

app = App()

@app.rpc("/users.get")
def get_user(request, payload):
    user = USERS.get(payload["id"])
    if user is None:
        raise RpcError("usuario inexistente", code="not_found", status=404,
                       details={"id": payload["id"]})
    return {"id": user.id, "name": user.name, "avatar": user.avatar_bytes}
```

- El handler recibe `request` y el `payload` ya decodificado. Si el cuerpo
  llega vacio, `payload` es `None`.
- Lo que el handler devuelve se codifica con el mismo codec de la peticion, o
  con el que pida la cabecera `Accept`. Si la peticion no indica ninguno, se
  usa el valor de `codec`, que por defecto es `"cbor"`.
- Si el handler lanza `RpcError(message, code=..., status=..., details=...)`,
  el servidor responde ese status con el cuerpo
  `{"error": {"code", "message", "details"}}`.
- Cualquier otra excepcion produce un `500` con `code="internal"`, sin revelar
  detalles internos. La excepcion se registra en los logs de la app.
- Un `Content-Type` desconocido produce un `415` y un cuerpo que no se puede
  decodificar produce un `400`.
- `app.rpc()` admite las mismas opciones de workers que `app.route()`.

La misma app atiende RPC por HTTP/1.1, HTTP/2 y HTTP/3. Para HTTP/3 levanta un
`Http3Server` (UDP) junto al servidor TCP. Ambos pueden compartir el mismo
puerto (ver [versiones de HTTP](http-versions.md)):

```python
import threading

from wsbuilder import CertificateAuthority, CertificateManager
from wsbuilder.http3_server import Http3Server

ca = CertificateAuthority.create("Servicios internos")
tls = CertificateManager(ca=ca, common_name="users.internal",
                         dns_names=["users.internal"])
h3 = Http3Server("0.0.0.0", 8443, app, tls)
threading.Thread(target=h3.serve_forever, daemon=True).start()
app.run("0.0.0.0", 8443, ssl_context=tls.ssl_context())
```

## Cliente

```python
from wsbuilder import RpcClient, RpcError, RpcTransportError

with RpcClient("https://users.internal:8443", trust=ca) as users:
    user = users.call("/users.get", {"id": 7})
```

| Opcion | Uso |
| --- | --- |
| `base_url` | `https://host:puerto` usa HTTP/3; `http://host:puerto` usa HTTP/1.1 sin cifrar, pensado para desarrollo local. |
| `transport` | Fuerza `"http3"` o `"http1"`. `"http1"` con `https://` usa TLS sobre TCP. |
| `codec` | `"cbor"` (por defecto) o `"json"`. |
| `trust` / `ca_pem` | CA que debe firmar el certificado del servidor: un `CertificateAuthority` o un PEM. Si no se indica, se usa el almacen del sistema. |
| `client_material` | Certificado del cliente (`TLSMaterial`) para mTLS. |
| `server_name` | Nombre que se verifica en el certificado cuando no coincide con el host (por ejemplo, al llamar a una IP). |
| `timeout` | Segundos por llamada (10 por defecto). |
| `headers` | Cabeceras que se envian en todas las llamadas. |
| `max_response_bytes` | Limite del cuerpo de cada respuesta (64 MiB por defecto). |

- `call(path, payload=None, *, headers=None, timeout=None)` devuelve el
  resultado decodificado.
- Una respuesta de error lanza `RpcError` en el cliente con el mismo `status`,
  `code`, `message` y `details` que envio el servidor.
- Si no llega ninguna respuesta (conexion rechazada, timeout, certificado no
  valido, respuesta rota), se lanza `RpcTransportError`.
- El cliente es seguro entre hilos: todas las llamadas comparten la conexion
  HTTP/3. Con HTTP/1.1 cada hilo usa su propia conexion keep-alive.
- `describe()` informa del transporte, el codec, el numero de llamadas y los
  bytes enviados y recibidos.

HTTP/3 necesita el extra `tls` (`pip install 'wsbuilder[tls]'`). Sin ese extra,
`RpcClient` funciona con `http://` y `transport="http1"`.

## Identidad de cada servicio (mTLS)

Con TLS mutuo cada servicio demuestra quien es con un certificado de la CA
interna, y el servidor rechaza durante el handshake a quien no lo presente.

```python
# Servidor: exige certificados firmados por la CA interna.
tls = CertificateManager(ca=ca, common_name="users.internal",
                         dns_names=["users.internal"],
                         client_ca_pem=ca.certificate_pem,
                         require_client_cert=True)

@app.rpc("/users.get")
def get_user(request, payload):
    caller = request.tls["peer_cert"]   # verificado durante el handshake
    ...

# Cliente: presenta su propio certificado.
orders_cert = ca.issue("orders-service", client_auth=True, server_auth=False)
users = RpcClient("https://users.internal:8443", trust=ca,
                  client_material=orders_cert)
```

La misma configuracion `client_ca_pem` / `require_client_cert` se aplica a
HTTP/1.1, HTTP/2 y HTTP/3. Por HTTP/3, `request.tls["peer_cert"]` es un
diccionario con `common_name`, `dns_names`, `ip_addresses`, `uris`, `subject`,
`issuer`, `serial_number`, `not_valid_after` y `fingerprint_sha256`. Por
TCP conserva el formato de `ssl.getpeercert()`.

La verificacion de cadenas usa el verificador WebPKI de `cryptography`. Para
certificados de cliente sobre HTTP/3 hace falta `cryptography>=43`.

## Codec CBOR directo

El codec tambien se puede usar fuera de RPC:

```python
from wsbuilder import cbor_dumps, cbor_loads

data = cbor_dumps({"id": 7, "blob": b"\x00\x01", "tags": ["a", "b"]})
assert cbor_loads(data) == {"id": 7, "blob": b"\x00\x01", "tags": ["a", "b"]}
```

- Tipos soportados: `None`, `bool`, `int` (de cualquier tamano), `float`,
  `str`, `bytes`, `list`/`tuple` y `dict`. `CBORTag` y `CBORSimple` cubren
  etiquetas y valores simples.
- Cada valor se escribe en su forma mas corta. Por ejemplo, un `float` usa 16,
  32 o 64 bits segun haga falta para conservarlo exacto.
- La entrada se trata como no confiable. Se comprueban las longitudes antes de
  reservar memoria, la profundidad esta limitada (`max_depth`, 256 por
  defecto) y se rechazan los bytes sobrantes.

## Limites actuales

- No hay 0-RTT ni reanudacion de sesion: cada conexion QUIC nueva hace un
  handshake completo de 1-RTT. Como la conexion se reutiliza, esto solo afecta
  a la primera llamada y a cada rotacion.
- El servidor HTTP/3 de wsbuilder concede 100 streams por conexion. Al
  agotarlos, el cliente abre otra conexion de forma transparente.
- En una red local sin perdidas, el transporte HTTP/1.1 con keep-alive puede
  tener menos latencia por llamada que HTTP/3, porque su TLS esta escrito en C.
  HTTP/3 compensa cuando hay muchas llamadas concurrentes, perdida de paquetes
  o conexiones nuevas frecuentes.
