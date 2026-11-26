# Modelos de lenguaje (LLM)

!!! warning "Experimental"
    Los modulos de IA y modelos de lenguaje (`ia`, `ia_lm`, `ia_text`,
    `ia_audio`, `predicts`, `llm`, `ndjson_model`, `bpe`) son experimentales: su API y
    sus formatos pueden cambiar entre versiones menores sin periodo de
    transicion, a diferencia del nucleo HTTP/WebSocket. Fija la version
    (`wsbuilder==X.Y.*`) si dependes de ellos y revisa el
    [registro de cambios](changelog.md) al actualizar.

`wsbuilder.llm` permite entrenar, importar, guardar, descargar y ejecutar
modelos de lenguaje. Tambien puede servirlos por HTTP, con un endpoint
compatible con la API de OpenAI. Los modelos se ejecutan con el **motor nativo
de wsbuilder**, escrito en Python puro: no hace falta llama.cpp ni ningun otro
motor externo.

| Pieza | Para que sirve | Dependencias |
| --- | --- | --- |
| `ByteLanguageModel` | Modelo de lenguaje nativo: se entrena, genera texto y se guarda en NDJSON | Ninguna |
| `LanguageModel` + `NativeBackend` | Conversar con un modelo nativo (`chat`, `generate`, streaming) | Ninguna |
| `ModelWriter` / `ModelReader` | Escribir y leer el formato de modelos de wsbuilder: un unico archivo NDJSON con el modelo y sus pesos | Ninguna |
| `ModelStore` | Directorio de modelos: importar, descargar, exportar, verificar y borrar | Ninguna |
| `download_file`, `huggingface_files` | Descargas reanudables y verificadas con SHA-256 | Ninguna |
| `LanguageModel` + `OpenAICompatibleBackend` | Opcional: usar un modelo que ya sirve otro programa por HTTP (API de OpenAI) | Ninguna |
| `install_llm` / `App.enable_llm` | Exponer los modelos por HTTP | Ninguna |

Todo funciona solo con la biblioteca estandar.

## Inicio rapido: entrenar y conversar

```python
from wsbuilder import ByteLanguageModel, LanguageModel

conversaciones = [
    [{"role": "user", "content": "hola"}, {"role": "assistant", "content": "Hola, en que te ayudo?"}],
    [{"role": "user", "content": "precio"}, {"role": "assistant", "content": "El plan cuesta 10 euros."}],
]

modelo = ByteLanguageModel(context=16, embedding=8, hidden=(64,), seed=1)
modelo.fit_chat(conversaciones, epochs=60, learning_rate=0.1, decay=0.97)
modelo.chat("precio", temperature=0)          # "El plan cuesta 10 euros."
modelo.save("bot.ndjson", name="bot")         # modelo + pesos en un NDJSON

bot = LanguageModel.from_file("bot.ndjson", temperature=0.7)
print(bot.chat("hola"))
```

## El motor nativo: `ByteLanguageModel`

Es el modelo de lenguaje propio de wsbuilder (arquitectura `wsbuilder.bytelm`).
Lee los ultimos `context` bytes del texto UTF-8 y predice el siguiente byte con
una red neuronal: un vector por byte (`embedding`), una o varias capas `tanh`
(`hidden`) y un softmax sobre los 256 bytes mas un token de fin de texto.

| Metodo | Uso |
| --- | --- |
| `ByteLanguageModel(context=24, embedding=12, hidden=(96,), seed=None)` | Crea un modelo con pesos aleatorios. |
| `fit(textos, epochs=10, learning_rate=0.05, decay=0.9, progress=None)` | Entrena con textos. Devuelve el historial `{"epoch", "loss", "learning_rate"}`. |
| `fit_chat(conversaciones, ...)` | Entrena con conversaciones `[{"role", "content"}, ...]`. |
| `generate(prompt, **opciones)` / `stream(prompt, **opciones)` | Continua un texto, entero o por partes. |
| `chat(messages, **opciones)` | Responde a una conversacion. |
| `perplexity(texto)` | Mide lo bien que el modelo predice un texto (menos es mejor). |
| `save(ruta, dtype="F32", name=None)` / `ByteLanguageModel.load(ruta)` | Guarda y carga el modelo en NDJSON. |

Opciones de generacion: `max_tokens` (en bytes, 256 por defecto),
`temperature` (`0` elige siempre el byte mas probable), `top_k`, `top_p`,
`repeat_penalty`, `stop` (texto o lista de textos) y `seed`.

- **Formato de chat.** `format_chat(messages)` convierte la conversacion en
  `<|user|>hola\n<|assistant|>`. El modelo aprende ese formato con `fit_chat` y
  la respuesta termina en el token de fin de texto o antes del siguiente
  `\n<|`.
- **Entrenamiento en segundo plano.** `submit_lm_training_task(app.tasks, modelo,
  textos, epochs=...)` entrena en una tarea de `TaskManager`; su `metadata`
  muestra `epoch` y `loss` mientras avanza.
- **Para que sirve.** Es un modelo pequeno pensado para corpus pequenos y
  concretos: respuestas de un asistente, comandos, preguntas frecuentes,
  frases de un dominio. Solo ve los ultimos `context` bytes, asi que no
  mantiene conversaciones largas ni conocimiento general.
- **Rendimiento.** Es Python puro: el coste de entrenar crece con
  `context * embedding * hidden` por cada byte de texto y epoca. Con
  `context=16, embedding=8, hidden=(64,)`, cada byte de entrenamiento tarda
  unos 2-3 ms y generar una respuesta corta, milisegundos. Empieza con
  tamanos pequenos y entrena en una tarea.

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

Los modelos nativos guardados en el almacen se ejecutan sin configurar nada:

```python
from wsbuilder import ModelStore

store = ModelStore()
record = store.add(modelo, name="Mi bot")      # guarda un ByteLanguageModel en el almacen
bot = store.load(record.id, temperature=0.7)   # LanguageModel nativo, cacheado por id
print(bot.chat("Hola"))
for piece in bot.stream_chat([{"role": "user", "content": "precio"}], max_tokens=64):
    print(piece, end="", flush=True)
```

Las opciones de `store.load` son las opciones de generacion por defecto de ese
modelo. Un modelo guardado que no es de lenguaje (por ejemplo una red
`wsbuilder.dense`) no se puede usar para conversar: `store.load` lanza
`NotImplementedError` y la API HTTP responde 501. Tambien puedes crear un
`LanguageModel` directamente con `LanguageModel.native(modelo)` o
`LanguageModel.from_file("bot.ndjson")`.

Opcionalmente, un modelo que ya sirve otro programa con la API de OpenAI se usa
con `LanguageModel.from_openai("http://localhost:11434/v1", "nombre")`.
`ModelStore(loader=...)` permite sustituir el motor nativo por uno propio:
`loader(record, **opciones)` recibe el `ModelRecord` y devuelve un
`LanguageModel`.

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
from wsbuilder import App, ModelStore

app = App()
app.enable_security(...)  # protege estas rutas antes de exponerlas
app.enable_llm(
    ModelStore(),
    backend_options={"temperature": 0.7},   # opciones por defecto de los modelos nativos
    allow_train=True,
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
- `allow_train=True` activa `POST /api/llm/train`: entrena un modelo nativo en
  segundo plano y lo guarda en el almacen. Recibe `{"name", "texts"}` o
  `{"name", "conversations"}` y, opcionalmente, `context`, `embedding`,
  `hidden`, `epochs`, `learning_rate` y `seed`. Responde `{"task"}`; en
  `GET /api/llm/tasks?id=` se ven `epoch` y `loss` y, al terminar, el
  manifiesto del modelo nuevo. `train_limits` ajusta los limites de
  `DEFAULT_TRAIN_LIMITS` (1 MiB de texto, `context` 128, `embedding` 64,
  capas de hasta 512 unidades, 500 epocas).

## Tokenizador BPE (Qwen3, GPT-2, Llama 3)

`wsbuilder.bpe` incluye `BPETokenizer`, un tokenizador BPE a nivel de bytes
compatible con los de GPT-2, Qwen2/Qwen3 y Llama 3, escrito solo con la
biblioteca estandar. Lee el `tokenizer.json` de Hugging Face o los metadatos
`tokenizer.ggml.*` de un GGUF (ya leidos como `dict`):

```python
from wsbuilder import BPETokenizer, format_qwen3_chat

tok = BPETokenizer.from_pretrained_dir("Qwen3-0.6B")    # tokenizer.json + tokenizer_config.json
# tok = BPETokenizer.from_file("tokenizer.json")
# tok = BPETokenizer.from_gguf_metadata(metadata)         # tokenizer.ggml.model == "gpt2"

prompt = format_qwen3_chat(
    [{"role": "user", "content": "Hola"}],
    enable_thinking=False,
)
ids = tok.encode(prompt)                  # los <|im_start|>/<|im_end|> son un solo id
text = tok.decode(ids, skip_special_tokens=True)

stream = tok.stream_decoder()             # para streaming: retiene UTF-8 incompleto
pieces = [stream.push(token_id) for token_id in ids]
```

- `encode(text, add_special_tokens=False, allow_special=True)` separa primero
  los tokens anadidos por coincidencia mas larga; con `allow_special=False` los
  especiales se tokenizan como texto normal. `decode(ids,
  skip_special_tokens=False)` sustituye el UTF-8 invalido por U+FFFD.
- Normalizadores soportados: ninguno, `NFC`, `NFD`, `NFKC`, `NFKD` y `Sequence`
  de ellos. Pretokenizadores reconocidos: los patrones de Qwen2/Qwen3, Llama 3
  y GPT-2, implementados sin el modulo `regex`; con otro patron se lanza
  `ValueError`, salvo que se pase `pretokenizer=` (un nombre o una funcion).
- `from_gguf_metadata` no normaliza, como llama.cpp; pasa `normalizer="NFC"`
  para reproducir el tokenizador de Hugging Face de Qwen. Los tokens de tipo 5
  (sin uso, como `[PAD...]`) no se generan y se decodifican como texto vacio.
- `format_qwen3_chat(messages, add_generation_prompt=True,
  enable_thinking=True, system=None)` genera el prompt ChatML de Qwen3 sin
  Jinja y quita el bloque `<think>` de las respuestas anteriores a la ultima
  pregunta. `system=` anade un primer mensaje de sistema (`ValueError` si los
  mensajes ya empiezan por uno). Los roles `tool` y las llamadas a
  herramientas aun no estan soportados.

## Redes propias

Las redes de `NeuralNetwork` se guardan en el mismo formato NDJSON, con la
arquitectura `wsbuilder.dense`:

```python
network.save("xor.ndjson", name="xor")          # dtype="F64" (exacto), "F32" o "F16"
same = NeuralNetwork.load("xor.ndjson")          # mismas predicciones

record = store.import_file("xor.ndjson")
same = store.load_network(record.id)
```
