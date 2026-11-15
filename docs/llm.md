# Modelos de lenguaje (LLM)

`wsbuilder.llm` permite importar, guardar, descargar y ejecutar modelos de
lenguaje. Tambien puede servirlos por HTTP, con un endpoint compatible con la
API de OpenAI.

| Pieza | Para que sirve | Dependencias |
| --- | --- | --- |
| `GGUFReader` / `GGUFWriter` | Leer y escribir archivos GGUF, el formato de llama.cpp y Ollama | Ninguna |
| `ModelStore` | Directorio de modelos: importar, descargar, exportar, verificar y borrar | Ninguna |
| `download_file`, `huggingface_files` | Descargas reanudables y verificadas con SHA-256 | Ninguna |
| `LanguageModel` + `OpenAICompatibleBackend` | Usar un modelo servido por Ollama, llama.cpp server, vLLM, LM Studio u OpenAI | Ninguna |
| `LanguageModel` + `LlamaCppBackend` | Ejecutar un GGUF local en este proceso | `pip install 'wsbuilder[llm]'` |
| `install_llm` / `App.enable_llm` | Exponer los modelos por HTTP | Ninguna |

Para ejecutar un LLM hace falta un motor de inferencia escrito en C/C++, y
wsbuilder no intenta serlo. Todo lo demas funciona solo con la biblioteca
estandar.

## Guardar modelos: el almacen

```python
from wsbuilder import ModelStore

store = ModelStore()          # $WSBUILDER_MODELS o ~/.cache/wsbuilder/models
record = store.import_file("descargas/qwen2.5-0.5b-instruct-q4_k_m.gguf")
print(record.id, record.summary["architecture"], record.summary["quantization"])

for model in store.list():
    print(model.id, model.format, model.size, model.sha256)

store.export(record.id, "/backup/")        # copia el archivo fuera del almacen
store.verify(record.id)                    # recalcula el SHA-256
store.rename(record.id, "Qwen 0.5B")
store.remove(record.id)
```

- Cada modelo vive en `<raiz>/<id>/` junto a un `manifest.json` con su nombre,
  formato, tamano, SHA-256, origen y un resumen de sus metadatos. Como no hay
  un indice compartido, no hay nada comun que se pueda corromper. Las
  escrituras pasan por archivos temporales y `os.replace`.
- Se reconocen GGUF y safetensors. Otros formatos se rechazan salvo que se
  pase `require_known_format=False`. Un archivo malformado nunca entra al
  almacen.
- `import_file(..., move=True)` mueve el archivo en vez de copiarlo.

En un GGUF, el resumen incluye `name`, `architecture`, `parameters`,
`quantization` (por ejemplo `Q4_K_M`), `context_length`, `embedding_length`,
`block_count`, `attention_heads`, `tokenizer`, `vocabulary_size`,
`chat_template`, `license` y `file_size`.

## Descargar modelos

Desde Hugging Face:

```python
from wsbuilder import ModelStore, huggingface_files

files = huggingface_files("Qwen/Qwen2.5-0.5B-Instruct-GGUF", suffix=".gguf")
# [{"path": "qwen2.5-0.5b-instruct-q4_k_m.gguf", "size": 491400032, "sha256": "..."}, ...]

store = ModelStore()
record = store.download_huggingface(
    "Qwen/Qwen2.5-0.5B-Instruct-GGUF",
    "qwen2.5-0.5b-instruct-q4_k_m.gguf",
    progress=lambda done, total: print(done, total),
)
```

Desde cualquier URL HTTPS:

```python
record = store.download("https://example.com/modelo.gguf", sha256="...")
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

Un GGUF local con llama.cpp (`pip install 'wsbuilder[llm]'`):

```python
model = store.load(record.id, n_ctx=4096, n_gpu_layers=0)   # se cachea por id
print(model.chat("Explica que es QUIC en una frase"))
for piece in model.stream_chat([{"role": "user", "content": "Hola"}], max_tokens=64):
    print(piece, end="", flush=True)
```

Un modelo servido por Ollama u otro servidor compatible con OpenAI:

```python
from wsbuilder import LanguageModel

model = LanguageModel.from_openai("http://localhost:11434/v1", "llama3.2")
print(model.chat("Hola"))
```

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
    backend_options={"n_ctx": 4096},
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

## Archivos GGUF y redes propias

```python
from wsbuilder import GGUFReader, GGUFWriter

with GGUFReader("modelo.gguf") as gguf:
    gguf.metadata["general.architecture"]
    gguf.summary()
    gguf.tensor("token_embd.weight").shape

writer = GGUFWriter(architecture="mi-modelo")
writer.add_metadata("general.name", "demo")
writer.add_tensor("capa.0.weight", [0.1, 0.2, 0.3, 0.4], shape=(2, 2), ggml_type="F16")
writer.write("demo.gguf")
```

- **Lectura sin cargar pesos.** El lector solo lee la cabecera y las tablas,
  asi que abrir un modelo de varios GB es inmediato. Los arrays largos, como
  el vocabulario, quedan como `GGUFArray` (tipo y longitud) salvo que se pase
  `max_array_items=None`.
- **Decodificacion de tensores.** `tensor.values()` decodifica F32, F16, BF16,
  F64, I8 a I64, Q8_0 y Q4_0. Las cuantizaciones K e IQ se leen como bytes
  crudos.
- **Entrada no confiable.** Cada conteo y cada longitud se comprueban contra el
  tamano del archivo antes de reservar memoria.

Las redes de `NeuralNetwork` tambien se guardan en GGUF:

```python
network.save("xor.gguf", name="xor")          # dtype="F64" (exacto), "F32" o "F16"
same = NeuralNetwork.load("xor.gguf")          # mismas predicciones
```
