# TLS y certificados

`wsbuilder.pki` crea una autoridad certificadora, emite certificados y los rota.
Necesita el extra opcional:

```bash
python -m pip install "wsbuilder[tls]"
```

## Por que existe este modulo

`ssl.SSLContext.load_cert_chain()` solo acepta **rutas de archivo**: OpenSSL no
lee un certificado ni una clave desde memoria. Un certificado guardado en una
fila de base de datos, en un gestor de secretos o construido al arrancar tiene
que pasar por el disco antes de poder cifrar nada.

Este modulo reduce esa ventana al minimo. El PEM se escribe en un directorio
temporal privado (`0700`) con permisos `0600`, se entrega a OpenSSL y se borra
acto seguido: el `SSLContext` ya tiene su propia copia.

!!! warning "Sobre el borrado"
    Los bytes se sobrescriben antes de desenlazar el archivo, lo que impide
    leerlo despues por esa ruta. **No es una garantia de destruccion.** Un
    sistema de archivos con copy-on-write, un journal o el nivelado de desgaste
    de un SSD pueden conservar los bloques antiguos. Trata una maquina que
    ejecuta esto como una maquina que ha visto la clave.

## Arranque rapido

```python
from wsbuilder import App

app = App()
manager = app.enable_tls(common_name="localhost")
app.run("127.0.0.1", 8443, ssl_context=manager)
```

Sin argumentos crea una CA local desechable, suficiente para desarrollo.

## Crear la CA y emitir

```python
from wsbuilder import CertificateAuthority

ca = CertificateAuthority.create("Mi CA", organization="Acme", valid_days=3650)

leaf = ca.issue(
    "api.example.test",
    dns_names=["api.example.test", "alt.example.test"],
    ip_addresses=["127.0.0.1"],
    valid_days=30,
)
```

Ambas mitades son PEM plano, asi que guardarlas es escribir dos columnas:

```python
ca.certificate_pem      # bytes
ca.private_key_pem      # bytes
ca.private_key_pem_encrypted("passphrase")   # cifrada, si va a viajar

CertificateAuthority.load(cert_pem, key_pem)              # recuperar
CertificateAuthority.load(cert_pem, key_pem, password=".")  # si estaba cifrada
```

Si no indicas `dns_names`, el nombre comun se copia al SAN: los verificadores
dejaron de mirar el common name, asi que un certificado sin SAN no coincide con
nada. Una hoja nunca se emite mas alla de la caducidad de su CA.

## Estatico o rotativo

`CertificateManager` guarda el material en uso. El modo lo decide `rotate`:

| Modo | Cuando reemplaza |
| --- | --- |
| estatico (`rotate=False`) | solo cuando el certificado ya caduco |
| rotativo (`rotate=True`) | cuando faltan menos de `renew_before_seconds` |

De donde sale el material es independiente del modo. `provider` se consulta
primero; si devuelve algo con vida suficiente, se usa tal cual. Si no, el
manager emite uno nuevo y se lo pasa a `on_rotate`.

## El caso completo: material en base de datos

Es la forma para la que esta pensado el modulo. El PEM vive en la base de datos,
toca disco solo como archivo temporal, y la rotacion reescribe la fila.

```python
import sqlite3
from wsbuilder import App, CertificateAuthority, CertificateManager, TLSMaterial

db = sqlite3.connect("tls.db", check_same_thread=False)
db.execute("CREATE TABLE IF NOT EXISTS tls (name TEXT PRIMARY KEY, cert BLOB, key BLOB, chain BLOB)")

def cargar():
    row = db.execute("SELECT cert, key, chain FROM tls WHERE name = ?", ("localhost",)).fetchone()
    return None if row is None else TLSMaterial(row[0], row[1], row[2])

def guardar(material):
    db.execute(
        "INSERT OR REPLACE INTO tls VALUES (?, ?, ?, ?)",
        ("localhost", material.certificate_pem, material.private_key_pem, material.chain_pem),
    )
    db.commit()

ca = CertificateAuthority.create("Mi CA")
manager = CertificateManager(
    ca=ca,
    common_name="localhost",
    dns_names=["localhost"],
    ip_addresses=["127.0.0.1"],
    provider=cargar,      # reutiliza lo guardado entre reinicios
    on_rotate=guardar,    # escribe cada certificado nuevo
    rotate=True,
    valid_days=1,
    renew_before_seconds=3600,
)

app = App()
app.run("127.0.0.1", 8443, ssl_context=manager)
```

En el primer arranque no hay fila: el manager emite y `guardar` la crea. En el
siguiente reinicio `cargar` devuelve ese mismo certificado y **no** se emite
otro. Cuando quedan menos de 3600 segundos de vida, se emite uno nuevo y la fila
se reescribe.

El servidor pide el contexto **por conexion**, asi que una rotacion llega a las
conexiones nuevas sin reiniciar el proceso.

## Usar el material fuera del servidor

Para herramientas que solo aceptan rutas:

```python
with manager.materialize() as files:
    subprocess.run(["curl", "--cert", files.certificate, "--key", files.private_key, url])
```

Al salir del bloque los archivos se borran. Si olvidas cerrarlo, un finalizador
los borra al recolectar el objeto y, en el peor caso, al terminar el proceso.

## Autenticacion de cliente (mTLS)

```python
manager = CertificateManager(
    ca=ca,
    common_name="localhost",
    client_ca_pem=ca.certificate_pem,
    require_client_cert=True,
)
```

Con `require_client_cert=False` el certificado de cliente se acepta pero no se
exige.

## Observabilidad

Con `app.enable_metrics()` activo, el snapshot incluye un bloque `tls`:

```json
{
  "tls": {
    "mode": "rotating",
    "rotations": 3,
    "certificate": {
      "subject": "CN=localhost",
      "not_valid_after": "2026-09-21T15:05:06+00:00",
      "seconds_until_expiry": 82000.0,
      "dns_names": ["localhost"]
    }
  }
}
```

## Referencia

| Simbolo | Uso |
| --- | --- |
| `CertificateAuthority.create(...)` | crear una raiz autofirmada |
| `CertificateAuthority.load(cert, key, password=None)` | recuperar una CA guardada |
| `CertificateAuthority.issue(...)` | emitir una hoja firmada |
| `TLSMaterial` | certificado + clave + cadena, como PEM |
| `TLSMaterial.materialize()` | escribirlo a archivos temporales privados |
| `TLSMaterial.ssl_context(...)` | construir un `SSLContext` |
| `CertificateManager` | material en uso, estatico o rotativo |
| `install_tls(app, ...)` / `app.enable_tls(...)` | dejarlo en `app.tls` |
