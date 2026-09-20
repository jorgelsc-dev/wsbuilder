# Versiones de HTTP

El servidor habla **HTTP/0.9, 1.0, 1.1, 2 y 3**. Las cuatro primeras comparten
el puerto TCP; HTTP/3 necesita un listener UDP aparte, porque QUIC no corre
sobre TCP.

## Como se elige la version

| Version | Como se detecta |
| --- | --- |
| 0.9 | linea de peticion sin token de version (`GET /ruta`) |
| 1.0 / 1.1 | token de version en la linea de peticion |
| 2 sobre TLS | ALPN negocia `h2` |
| 2 en claro | la conexion abre con el preface de HTTP/2 |
| 3 | datagrama QUIC en el socket UDP, ALPN `h3` |

`request.version` dice cual llego, asi que un handler puede distinguirlas sin
mirar cabeceras.

```python
@app.api("/version")
def version(request):
    return {"http": request.version}
```

## HTTP/0.9

La peticion es una linea y la respuesta es el cuerpo desnudo: sin linea de
estado, sin cabeceras. El final lo marca el cierre de la conexion. Solo define
`GET`.

```bash
printf 'GET /version\r\n' | nc localhost 8000
```

## HTTP/1.1

Completo: cuerpos `chunked` con seccion de trailers, y conexiones persistentes
segun RFC 9112 seccion 9.3 -- por defecto en 1.1, opt-in en 1.0.

```python
@app.api("/subir", methods=("POST",))
def subir(request):
    # Un cuerpo chunked llega ya reensamblado.
    return {"bytes": len(request.body), "trailers": request.trailers}
```

| Atributo | Uso |
| --- | --- |
| `HTTPServer.KEEPALIVE_TIMEOUT_SECONDS` | espera entre peticiones reutilizadas |
| `HTTPServer.MAX_KEEPALIVE_REQUESTS` | peticiones por conexion; `1` desactiva la reutilizacion |

## HTTP/2

Tramas binarias multiplexadas con compresion HPACK. Se activa solo.

```python
server = HTTPServer("0.0.0.0", 8443, app, ssl_context=app.tls)
server.MAX_CONCURRENT_STREAMS = 100   # lo que anunciamos en SETTINGS
server.ENABLE_HTTP2 = False           # si prefieres solo HTTP/1
```

Para que ALPN ofrezca `h2`, dilo al construir el material TLS:

```python
manager = CertificateManager(
    ca=ca, common_name="localhost",
    alpn_protocols=["h2", "http/1.1"],
)
```

Notas de comportamiento que conviene conocer:

- Un handler que lanza responde **500**, igual que en HTTP/1, en vez de
  `RST_STREAM`. Se mantiene la coherencia con el resto del framework.
- Una respuesta mayor que la ventana del par **se encola** y sale cuando llega
  credito. Bloquear pararia todos los demas streams de la conexion.
- `PRIORITY` se valida pero no se usa: RFC 9113 lo declara obsoleto.
- El push del servidor esta desactivado (`ENABLE_PUSH: 0`).

## HTTP/3

QUIC sobre UDP, con TLS 1.3 integrado en el transporte.

```python
from wsbuilder.http3_server import Http3Server, alt_svc_header

h3 = Http3Server("0.0.0.0", 8443, app, app.tls)
threading.Thread(target=h3.serve_forever, daemon=True).start()
```

Un cliente no descubre HTTP/3 solo: llega por la cabecera `Alt-Svc` que anuncia
una respuesta HTTP/1 o HTTP/2.

```python
@app.api("/algo")
def algo(request):
    return Response.json({"ok": True}, headers={"Alt-Svc": alt_svc_header(8443)})
```

### Por que TLS 1.3 esta escrito a mano

QUIC **no transporta registros TLS**. Los mensajes de handshake viajan dentro de
frames CRYPTO, y el transporte deriva sus claves de proteccion de paquete del
key schedule de TLS. Hace falta introducir octetos de handshake y extraer
secretos de trafico en cada etapa, y el modulo `ssl` de Python no expone ninguna
de las dos cosas. Por eso existe `wsbuilder/quic/tls.py`.

### Limites que debes conocer antes de usarlo

Esto importa mas que la lista de lo que funciona:

- Sin 0-RTT, sin reanudacion de sesion, sin migracion de conexion, sin Retry.
- QPACK no usa tabla dinamica: se anuncia capacidad cero. Es un modo valido del
  RFC 9204, pero cuesta compresion en cabeceras propias repetidas.
- TLS 1.3 con una sola suite (`TLS_AES_128_GCM_SHA256`) y un solo grupo
  (X25519), solo servidor, sin certificados de cliente.

### Recuperacion de perdidas

Implementada segun RFC 9002. Un paquete se declara perdido por **orden** --
tres paquetes posteriores acusados, que es evidencia mas rapida que un reloj --
o por **tiempo**, pasado 9/8 del mayor entre el RTT suavizado y el ultimo
medido. Los espacios de numeracion son independientes: acusar un paquete 1-RTT
no dice nada de uno Initial.

Recuperar significa meter los frames del paquete perdido en uno **nuevo**, no
reenviar el paquete: un numero de paquete se usa una sola vez. Los frames que
solo describian el pasado, como un ACK, se descartan al retransmitir.

El control de congestion es NewReno: slow start hasta la primera perdida, luego
crecimiento lineal, con la ventana partida por la mitad y un unico evento de
congestion por RTT.

```python
app.enable_metrics()   # el snapshot incluye rtt, ventana y perdidas
```

Un paquete que solo lleva un ACK nunca arma un temporizador: el par no debe
acuse por el, asi que sondearlo no terminaria nunca.

Trata esto como una implementacion correcta del camino comun, con recuperacion
de perdidas real, pero sin las piezas que listo arriba.

## Que se verifico contra los RFC

| Capa | Comprobacion |
| --- | --- |
| HPACK | vectores del apendice C de RFC 7541, incluido el desalojo |
| varints QUIC | apendice A.1 de RFC 9000, incluida la forma no canonica |
| Proteccion de paquetes | apendice A de RFC 9001: secretos, claves AES y ChaCha20, mascaras de cabecera |
| TLS 1.3 | ClientHellos reales de OpenSSL; key schedule contra las constantes publicadas |
| HTTP/3 | peticion y respuesta sobre UDP real |
