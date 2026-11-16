# Modelos de lenguaje (LLM)

`wsbuilder.llm` permite importar, guardar, descargar y ejecutar modelos de
lenguaje. Tambien puede servirlos por HTTP, con un endpoint compatible con la
API de OpenAI.

| Pieza | Para que sirve | Dependencias |
| --- | --- | --- |
| `ModelWriter` / `ModelReader` | Escribir y leer el formato de modelos de wsbuilder: un unico archivo NDJSON con el modelo y sus pesos | Ninguna |
| `ModelStore` | Directorio de modelos: importar, descargar, exportar, verificar y borrar | Ninguna |
| `download_file`, `huggingface_files` | Descargas reanudables y verificadas con SHA-256 | Ninguna |
| `LanguageModel` + `OpenAICompatibleBackend` | Usar un modelo servido por Ollama, llama.cpp server, vLLM, LM Studio u OpenAI | Ninguna |
| `ModelStore(loader=...)` | Conectar los modelos guardados a tu propio motor de inferencia | La que use tu motor |
| `install_llm` / `App.enable_llm` | Exponer los modelos por HTTP | Ninguna |

Todo funciona solo con la biblioteca estandar.

## El formato de modelos: NDJSON

wsbuilder usa un solo formato para los modelos y sus pesos: un archivo
**NDJSON** (JSON con un objeto por linea, extension `.ndjson`). No se aceptan
GGUF, safetensors ni otros formatos.

- **Linea 1, el modelo.** Formato, version, arquitectura, nombre, metadatos
  libres y el numero de tensores que siguen.
- **Lineas siguientes, los pesos.** Un tensor por linea con su nombre, su forma
  (`shape`), su tipo (`dtype`) y sus valores en `data`, en una lista plana y en
  orden por filas.

```text
{"format":"wsbuilder-model","version":1,"architecture":"wsbuilder.dense","name":"xor","metadata":{"loss":"mse","activations":["tanh","sigmoid"]},"tensor_count":4}
{"tensor":"blk.0.weight","shape":[6,2],"dtype":"F64","data":[0.41,-1.2,...]}
{"tensor":"blk.0.bias","shape":[6],"dtype":"F64","data":[0.03,...]}
{"tensor":"blk.1.weight","shape":[1,6],"dtype":"F64","data":[...]}
{"tensor":"blk.1.bias","shape":[1],"dtype":"F64","data":[-0.2]}
```

```python
from wsbuilder import ModelReader, ModelWriter

writer = ModelWriter(architecture="mi-modelo", name="demo")
writer.add_metadata("context_length", 2048)            # cualquier valor JSON
writer.add_tensor("capa.0.weight", [[0.1, 0.2], [0.3, 0.4]], dtype="F32")
writer.add_tensor("capa.0.bias", [0.0, 0.0])
writer.write("demo.ndjson")                            # escritura atomica

with ModelReader("demo.ndjson") as model:
    model.architecture, model.name, model.metadata
    model.summary()                                    # parametros, dtypes, tamano...
    model.tensor("capa.0.weight").shape                # (2, 2)
    model.tensor("capa.0.weight").rows()               # [[0.1, 0.2], [0.3, 0.4]] (redondeado a F32)
```

- **Tipos.** `F64` (exacto), `F32` y `F16` (los valores se redondean a esa
  precision al escribir), `I32` e `I64`. NaN e infinito, que JSON no admite,
  se escriben como `"NaN"`, `"Infinity"` y `"-Infinity"`.
- **Lectura por partes.** Al abrir, `ModelReader` valida cada linea (nombres,
  tipos, formas y cantidad de valores) y recuerda donde empieza cada tensor;
  `tensor(nombre).values()` solo vuelve a leer esa linea.
- **Entrada no confiable.** Un archivo con lineas que no son JSON, tensores
  repetidos, valores que no cuadran con la forma o un `tensor_count` distinto
  del real se rechaza con `ModelFormatError`.
- **Portable.** Es texto plano: cualquier lenguaje con un parser JSON puede
  leerlo linea a linea, y `head -1 modelo.ndjson` muestra el modelo.

## Guardar modelos: el almacen

```python
from wsbuilder import ModelStore

store = ModelStore()          # $WSBUILDER_MODELS o ~/.cache/wsbuilder/models
record = store.import_file("descargas/mi-modelo.ndjson")
print(record.id, record.summary["architecture"], record.summary["parameters"])

for model in store.list():
    print(model.id, model.format, model.size, model.sha256)

store.export(record.id, "/backup/")        # copia el archivo fuera del almacen
store.verify(record.id)                    # recalcula el SHA-256
store.rename(record.id, "Mi modelo")
with store.open(record.id) as reader:      # ModelReader sobre el archivo guardado
    reader.tensor("capa.0.weight").values()
store.remove(record.id)
```

- Cada modelo vive en `<raiz>/<id>/` junto a un `manifest.json` con su nombre,
  formato, tamano, SHA-256, origen y un resumen de sus metadatos. Como no hay
  un indice compartido, no hay nada comun que se pueda corromper. Las
  escrituras pasan por archivos temporales y `os.replace`.
- Solo se aceptan modelos NDJSON de wsbuilder. Otros archivos se rechazan
  salvo que se pase `require_known_format=False` (quedan guardados con formato
  `unknown`, pero no se pueden abrir). Un archivo malformado nunca entra al
  almacen.
- `import_file(..., move=True)` mueve el archivo en vez de copiarlo.

El resumen incluye `name`, `architecture`, `parameters`, `tensors`, `dtypes`
(valores por tipo), `metadata` y `file_size`.

## Descargar modelos

Desde Hugging Face:

```python
from wsbuilder import ModelStore, huggingface_files

files = huggingface_files("usuario/mi-modelo", suffix=".ndjson")
# [{"path": "mi-modelo.ndjson", "size": 1048576, "sha256": "..."}, ...]

store = ModelStore()
record = store.download_huggingface(
    "usuario/mi-modelo",
    "mi-modelo.ndjson",
    progress=lambda done, total: print(done, total),
)
```

Desde cualquier URL HTTPS:

```python
record = store.download("https://example.com/modelo.ndjson", sha256="...")
```

- **Verificacion.** `download_huggingface` toma de la API del Hub el SHA-256
  publicado y lo comprueba. Si no coincide, el archivo se borra y se lanza
  `DownloadError`.
- **Reanudacion.** Si la descarga se corta, el siguiente intento continua desde
  el archivo `.part` con una peticion `Range`. Solo se reanuda cuando hay un
  SHA-256 con el que verificar el resultado.
- **Limites.** Solo se aceptan URLs `https://`. `max_bytes` acota el tamano
  (200 GiB por defecto) y `cancel_event` permite cancelar la descarga.
- **Repositorios privados.** El token se toma de `token=` o de `HF_TOKEN` y se
  envia solo al host original: si el Hub redirige a su CDN, la cabecera
  `Authorization` no viaja. `HF_ENDPOINT` permite usar un mirror.

## Ejecutar modelos

Un modelo servido por Ollama u otro servidor compatible con OpenAI:

```python
from wsbuilder import LanguageModel

model = LanguageModel.from_openai("http://localhost:11434/v1", "llama3.2")
print(model.chat("Hola"))
```

Para conversar con un modelo guardado en el almacen, pasa un `loader` que lo
convierta en un `LanguageModel` con el motor de inferencia que elijas. Recibe
el `ModelRecord` (con `record.path` apuntando al archivo NDJSON) y las
opciones de `load`:

```python
def loader(record, **options):
    return LanguageModel(MiBackend(record.path, **options), name=record.id)

store = ModelStore(loader=loader)
model = store.load(record.id)              # se cachea por id
for piece in model.stream_chat([{"role": "user", "content": "Hola"}], max_tokens=64):
    print(piece, end="", flush=True)
```

Un backend es cualquier objeto con `complete(prompt, *, stream=False, **opciones)`,
`chat(messages, *, stream=False, **opciones)` y `close()`. Sin `loader`,
`store.load` lanza `NotImplementedError` y la API HTTP responde 501.

| Metodo | Devuelve |
| --- | --- |
| `generate(prompt, **opciones)` | El texto completado. |
| `chat(messages, **opciones)` | La respuesta del asistente. `messages` puede ser un texto o una lista de `{"role", "content"}`. |
| `chat_completion(messages, **opciones)` | `{"text", "finish_reason", "usage"}`. |
| `stream_generate`, `stream_chat` | Generadores que van entregando el texto. |

Las opciones admitidas son `max_tokens`, `temperature`, `top_p`, `top_k`,
`stop`, `seed`, `repeat_penalty`, `presence_penalty` y `frequency_penalty`.

## Servir modelos por HTTP

```python
from wsbuilder import App, LanguageModel, ModelStore

app = App()
app.enable_security(...)  # protege estas rutas antes de exponerlas
app.enable_llm(
    ModelStore(),
    models={"llama3.2": LanguageModel.from_openai("http://localhost:11434/v1", "llama3.2")},
)
app.run("0.0.0.0", 8765)
```

| Ruta | Uso |
| --- | --- |
| `GET /api/llm/models` | Lista los modelos del almacen y los remotos. |
| `GET /api/llm/model?id=` | Devuelve el manifiesto de un modelo. |
| `POST /api/llm/chat` | Recibe `{"model", "messages", "stream"}`. Con `stream`, responde en NDJSON (`{"content": ...}` por linea). |
| `POST /api/llm/generate` | Recibe `{"model", "prompt", "stream"}`. |
| `GET /v1/models`, `POST /v1/chat/completions` | API de OpenAI, con streaming SSE incluido. Cualquier cliente de OpenAI puede apuntar a la app. |

Las rutas que modifican el disco del servidor estan desactivadas por defecto:

- `allow_download=True` activa `POST /api/llm/download` (`{"repo", "filename"}`
  o `{"url", "sha256"}`). La descarga corre como tarea en segundo plano y su
  progreso se consulta en `GET /api/llm/tasks?id=`. `download_hosts`
  restringe los hosts permitidos.
- `allow_import=True` activa `POST /api/llm/import` (`{"path"}`).
- `allow_delete=True` activa `POST /api/llm/delete` (`{"id"}`).

## Redes propias

Las redes de `NeuralNetwork` se guardan en el mismo formato NDJSON, con la
arquitectura `wsbuilder.dense`:

```python
network.save("xor.ndjson", name="xor")          # dtype="F64" (exacto), "F32" o "F16"
same = NeuralNetwork.load("xor.ndjson")          # mismas predicciones

record = store.import_file("xor.ndjson")
same = store.load_network(record.id)
```
