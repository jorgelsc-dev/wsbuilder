# Modelos de lenguaje (LLM)

!!! warning "Experimental"
    Los modulos de IA y modelos de lenguaje (`ia`, `ia_lm`, `ia_text`,
    `ia_audio`, `predicts`, `llm`, `ndjson_model`, `weights`, `bpe`, `graph`,
    `graph_convert`) son experimentales: su API y
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
| `open_weights` | Leer pesos en safetensors o GGUF (mmap, sin copiar) | Ninguna |
| `convert_to_graph` + `GraphModel` | Convertir Qwen3, Qwen2, Llama o Mistral (Hugging Face o GGUF) a `model.json` + pesos NDJSON y ejecutarlos | Ninguna |
| `ModelStore` | Directorio de modelos: importar, descargar, convertir a grafo, ejecutar, exportar, verificar y borrar | Ninguna |
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
**NDJSON** (JSON con un objeto por linea, extension `.ndjson`). Los pesos en
safetensors o GGUF se pueden leer e importar al almacen (ver
[Leer pesos safetensors y GGUF](#leer-pesos-safetensors-y-gguf)) y, para las
arquitecturas soportadas, convertir a un modelo de grafo que si se ejecuta (ver
[Modelos de grafo](#modelos-de-grafo-modeljson)).

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

- **Tipos.** `F64` (exacto), `F32`, `F16` y `BF16` (los valores se redondean a
  esa precision al escribir), `I32` e `I64`, y los bloques ggml `Q8_0` y `Q4_0`
  (solo en base64; la ultima dimension debe ser multiplo de 32). NaN e
  infinito, que JSON no admite, se escriben como `"NaN"`, `"Infinity"` y
  `"-Infinity"`.
- **Tensores compactos (version 2).** Escribir numeros como texto es inviable
  para modelos grandes: `add_tensor(..., encoding="base64")` y
  `add_tensor_bytes(nombre, bytes, shape=, dtype=)` guardan
  `{"tensor", "shape", "dtype", "encoding": "base64", "data": "<base64>"}`, con
  los bytes little-endian (4/3 del tamano binario). `add_tensor_bytes` acepta
  tambien una funcion sin argumentos que devuelve los bytes al escribir, para
  convertir un tensor cada vez. Un archivo solo con listas JSON y tipos de la
  version 1 se sigue escribiendo como version 1; `ModelReader` lee ambos.
- **Decodificar.** `tensor.to_array()` devuelve un `array.array` (`"f"` para
  todos los tipos float y cuantizados); `row_array(i)` y `row_arrays()` leen
  una fila o todas por bloques de ~1 MiB sin cargar el tensor entero.
- **Lectura por partes.** Al abrir, `ModelReader` valida cada linea (nombres,
  tipos, formas y cantidad de valores; en base64, longitud, relleno y
  alfabeto) y recuerda donde empieza cada tensor; `tensor(nombre).values()`
  solo vuelve a leer esa linea. Las lecturas son seguras entre hilos.
- **Entrada no confiable.** Un archivo con lineas que no son JSON, tensores
  repetidos, valores que no cuadran con la forma o un `tensor_count` distinto
  del real se rechaza con `ModelFormatError`.
- **Portable.** Es texto plano: cualquier lenguaje con un parser JSON puede
  leerlo linea a linea, y `head -1 modelo.ndjson` muestra el modelo.

## Modelos de grafo (`model.json`)

Un **modelo de grafo** describe con datos lo que calcula un modelo de lenguaje
decoder, de modo que el motor nativo lo ejecuta sin codigo especifico de cada
arquitectura. Es un directorio con tres archivos:

- `model.json`: el grafo y la configuracion.
- `weights.ndjson`: los tensores en el [formato NDJSON](#el-formato-de-modelos-ndjson)
  (normalmente base64, con los nombres de GGUF: `token_embd.weight`,
  `blk.0.attn_q.weight`...).
- `tokenizer.json`: el tokenizador BPE (formato de Hugging Face).

```python
from wsbuilder import GraphModel, LanguageModel, convert_to_graph

path = convert_to_graph("Qwen3-0.6B", "qwen3-graph")      # directorio de Hugging Face
# path = convert_to_graph("qwen3-0.6b-q8_0.gguf", "qwen3-graph")
model = GraphModel.load(path, max_context=2048)
print(model.chat("Hola", enable_thinking=False, max_tokens=32, temperature=0))

llm = LanguageModel.from_graph("qwen3-graph", temperature=0.6)   # backend GraphBackend
llm.chat_completion([{"role": "user", "content": "Hola"}])        # usage con tokens reales
```

### Convertir

`convert_to_graph(source, out_dir, dtype=None, name=None)` acepta un
directorio de Hugging Face (`config.json`, `model.safetensors` o
`model.safetensors.index.json`, `tokenizer.json`, `tokenizer_config.json` y,
si existe, `generation_config.json`) o un archivo GGUF, y escribe los tres
archivos. Arquitecturas soportadas: `qwen3`, `qwen2`, `llama` y `mistral`; con
otra lanza `NotImplementedError` con la lista. Cada arquitectura es solo una
funcion que construye el JSON del grafo. Tambien se rechaza
(`NotImplementedError`) un modelo con tensores que el grafo no usaria (por
ejemplo biases del MLP) o con un escalado de RoPE no soportado (YaRN,
dinamico): mejor un error que logits incorrectos.

- Las matrices conservan su tipo si el motor lo lee (`F32`, `F16`, `BF16`,
  `Q8_0`, `Q4_0`, copiadas byte a byte) y pasan a `F32` en otro caso;
  `dtype="F32"|"F16"|"BF16"` las convierte. Normas y biases van siempre en
  `F32`. Los tensores se copian de uno en uno.
- El tokenizador de un GGUF se genera desde sus metadatos con
  `BPETokenizer.to_json()`.
- RoPE: los checkpoints de Hugging Face rotan los pares `(i, i + d/2)` (estilo
  `neox`). El conversor de llama.cpp permuta las filas de Q y K (y sus biases)
  de los modelos **llama** para rotar pares vecinos `(2i, 2i + 1)`: esos grafos
  usan el estilo `interleaved` con los pesos permutados. Qwen2/Qwen3 en GGUF no
  se permutan y usan `neox`. El escalado `llama3` de Llama 3.1 se guarda como
  `rope_freqs.weight` (divisores por frecuencia, en F32 como en llama.cpp).
- `generation_config.json` aporta los valores por defecto (`temperature`,
  `top_k`, `top_p`, `repetition_penalty`) y los ids de fin de secuencia.

### El formato `model.json`

```json
{"format": "wsbuilder-graph", "version": 1, "name": "Qwen3-0.6B",
 "architecture": "qwen3",
 "weights": "weights.ndjson",
 "tokenizer": {"type": "bpe", "file": "tokenizer.json"},
 "chat_template": "qwen3",
 "config": {"context_length": 40960, "bos_token_id": null, "add_bos_token": false,
            "eos_token_ids": [151645, 151643]},
 "generation": {"temperature": 0.6, "top_k": 20, "top_p": 0.95},
 "graph": {
   "embed": {"weight": "token_embd.weight"},
   "blocks": {"count": 28, "ops": [
     {"op": "rms_norm", "in": ["x"], "out": "h", "weight": "blk.{i}.attn_norm.weight", "eps": 1e-6},
     {"op": "linear", "in": ["h"], "out": "q", "weight": "blk.{i}.attn_q.weight"},
     "...",
     {"op": "add", "in": ["x", "m"], "out": "x"}]},
   "head": {"ops": [
     {"op": "rms_norm", "in": ["x"], "out": "x", "weight": "output_norm.weight", "eps": 1e-6},
     {"op": "linear", "in": ["x"], "out": "logits", "weight": "token_embd.weight"}]}}}
```

- `weights` y `tokenizer.file` son rutas relativas a `model.json`; una ruta
  absoluta o con `..` (o un enlace que salga del directorio) se rechaza.
- `chat_template`: `"qwen3"` (con `enable_thinking`), `"chatml"` (Qwen2),
  `"llama3"` o `null` (solo `generate`). El texto de los mensajes nunca se
  convierte en tokens de control. Las plantillas son las basicas: no anaden el
  mensaje de sistema por defecto de Qwen2.5 ni la fecha de corte de Llama 3.1.
- `config.eos_token_ids`, junto con `<|im_end|>`/`<|endoftext|>` (Qwen) o
  `<|eot_id|>`/`<|eom_id|>`/`<|end_of_text|>` (Llama 3), detiene la
  generacion. `add_bos_token` antepone `bos_token_id` al prompt.
- `generation` fija las opciones por defecto (`max_tokens`, `temperature`,
  `top_k`, `top_p`, `repeat_penalty`).
- El grafo se ejecuta una vez por token con cache de claves y valores. `embed`
  busca la fila del token (opcionalmente multiplicada por `scale`) en el valor
  `x`; las `ops` de `blocks` se ejecutan `count` veces con `{i}` sustituido en
  los nombres de tensor por el numero de bloque (sustitucion literal, no
  `str.format`) y deben dejar el flujo residual en `x`; las de `head` parten de
  `x` y deben producir `logits`.
- Al cargar se valida todo antes de ejecutar: op o atributo desconocido,
  tensor ausente, forma que no encaja, valor usado antes de definirse, tamanos
  incompatibles o claves desconocidas lanzan `GraphFormatError`.

Ops de la version 1 (`GRAPH_OPS`); cada una es
`{"op", "in": [valores], "out": valor, ...atributos}` y trabaja con vectores:

| Op | Entradas | Atributos | Calcula |
| --- | --- | --- | --- |
| `rms_norm` | 1 | `weight`, `eps` (1e-6), `add_unit_offset` (Gemma: `1 + w`) | RMSNorm |
| `layer_norm` | 1 | `weight`, `bias` opcional, `eps` (1e-5) | LayerNorm |
| `linear` | 1 | `weight` `[salidas, entradas]`, `bias` opcional | `W x + b` |
| `add`, `mul` | 2 | | Suma y producto elemento a elemento |
| `scale` | 1 | `value` | `x * value` |
| `silu`, `gelu`, `gelu_tanh`, `relu` | 1 | | Activaciones |
| `split` | 1 | `sizes`; `out` es una lista | Parte un vector (QKV o gate/up fusionados, Phi-3) |
| `head_rms_norm` | 1 | `weight` `[head_dim]`, `head_dim`, `eps`, `add_unit_offset` | RMSNorm por cabeza (Q/K de Qwen3) |
| `rope` | 1 | `head_dim`, `style` (`neox`/`interleaved`), `theta`, `rotary_dim` (parcial), `scaling` (`null` o `{"type": "linear", "factor"}`), `freq_factors` (tensor) | Rotary embeddings en la posicion del token |
| `attention` | 3 (q, k, v) | `heads`, `kv_heads` (GQA), `head_dim`, `scale` (`1/sqrt(head_dim)`), `sliding_window`, `logit_softcap` | Atencion causal con la cache KV |
| `softcap` | 1 | `cap` | `tanh(x / cap) * cap` (logits finales de Gemma 2) |

Cualquier arquitectura expresable con estas ops funciona sin tocar el motor;
otras (mezcla de expertos, atencion con estado, normas distintas) necesitan
ops nuevas y una nueva version del formato.

### Ejecutar

`GraphModel.load(path, max_context=None, accel=None)` acepta `model.json` o su
directorio; `accel` elige el backend de calculo (ver [Aceleracion](#aceleracion)).
`max_context` (por defecto el menor de `config.context_length` y 4096) limita
prompt + respuesta; un prompt mas largo lanza `ValueError` y la generacion se
detiene con `finish_reason="length"` al llegar al limite.

- `forward(token_id, position, cache)` devuelve los logits; `new_cache()` crea
  la cache de una secuencia y `position` debe ser `cache.length`.
- `generate`/`stream(prompt, **opciones)` continuan un texto o una lista de
  ids; `chat`/`stream_chat(messages, enable_thinking=True, system=None,
  **opciones)` usan la plantilla. Opciones: `max_tokens` (tokens reales),
  `temperature` (0 = greedy), `top_k`, `top_p`, `repeat_penalty` (como Hugging
  Face y llama.cpp: divide los logits positivos de los ultimos 64 tokens y
  multiplica los negativos), `stop` y `seed`.
- `generate` respeta los tokens de control escritos en el prompt (es la API de
  completado en crudo, como en Hugging Face): si lo expones por HTTP a usuarios
  no confiables, usa `chat`.
- `GraphBackend(model, **defaults)` tiene la interfaz de los demas backends
  (`complete`, `chat`, `close`; `usage` cuenta tokens) y
  `LanguageModel.from_graph(path)` lo envuelve. Las filas de logits que el
  tokenizador no sabe decodificar (relleno del vocabulario) nunca se eligen.
- `ModelStore` guarda y ejecuta modelos de grafo: `store.convert()`,
  `store.import_graph()` y `store.download_huggingface_model()` los registran y
  `store.load(id)` los ejecuta (ver [Modelos de grafo en el
  almacen](#modelos-de-grafo-en-el-almacen)). Un safetensors o GGUF suelto del
  almacen sigue lanzando `NotImplementedError`, con la sugerencia de
  convertirlo.

### Aceleracion

Los productos matriz-vector y la atencion se ejecutan en un *backend* de
`wsbuilder.accel`, todo codigo de wsbuilder sin bibliotecas de terceros (ni
numpy ni BLAS). El acelerador es generico: sirve para cualquier modelo de grafo
(`model.json` + `weights.ndjson`), sin rutas por arquitectura.

| Backend | Como funciona | Cuando |
| --- | --- | --- |
| `native` | `_accel_kernels.c` (incluido en el paquete) compilado al vuelo con el compilador del sistema y llamado con `ctypes`; reparte las filas de las matrices grandes entre hilos (`ctypes` suelta el GIL) | por defecto si hay compilador |
| `process` | pool de procesos con las matrices grandes en `multiprocessing.shared_memory`; cada proceso calcula un rango de filas con el bucle de Python | solo si se pide |
| `python` | `sum(map(operator.mul, fila, x))` por fila: la referencia | sin compilador, o si se pide |

```python
model = GraphModel.load("qwen3-graph")               # accel="auto"
model.accel_name                                      # "native", "process" o "python"
model = GraphModel.load("qwen3-graph", accel="python")
llm = LanguageModel.from_graph("qwen3-graph", accel="native")

from wsbuilder import accel
accel.describe()        # compilador, directorio de cache, biblioteca ya compilada
backend = accel.NativeAccel(threads=4)   # o ProcessAccel(workers=4), PythonAccel()
model = GraphModel.load("qwen3-graph", accel=backend)  # el llamador cierra backend
```

- **Seleccion**: `accel=` o la variable `WSBUILDER_ACCEL`
  (`auto|native|process|python`). `auto` usa `native` si compila y carga; si
  no, registra un aviso con `wsbuilder.logs` y usa `python`. Pedir `native`
  explicitamente sin compilador lanza `AccelError`. `process` nunca se elige
  solo: con `spawn`/`forkserver` (por defecto `forkserver` en POSIX) cada
  proceso trabajador vuelve a importar el script principal, que debe proteger
  su trabajo con `if __name__ == "__main__":`, como cualquier codigo de
  `multiprocessing`. Si los trabajadores no arrancan, calcula en el proceso
  principal y avisa.
- **Resultados**: `process` da exactamente los mismos numeros que `python`.
  `native` convierte cada peso al mismo valor float que los decodificadores de
  Python y acumula en `double`, pero suma en otro orden (ocho sumas parciales
  por fila): los logits difieren en el redondeo (~1e-14 relativo en las
  pruebas), y la generacion greedy de las pruebas coincide.
- **Memoria**: `native` guarda cada matriz en un unico buffer con el dtype del
  archivo (F32, F64, F16, BF16, Q8_0 o Q4_0) y la multiplica directamente:
  Q8_0 ocupa ~1,06 bytes por parametro y BF16/F16 2, en lugar de los 4 bytes
  de float32 de `python` y `process`. La cache KV es un `array("d")` contiguo
  por cabeza KV.
- `GraphModel.close()` libera matrices, hilos, procesos y memoria compartida
  (tambien al recolectar un `ProcessAccel` sin cerrar o al salir del
  interprete); un backend pasado por el llamador queda abierto.

**Compilacion y cache.** El compilador sale de `$CC`, del que se uso para
compilar Python (`sysconfig`), o de `cc`, `gcc` o `clang`, y se ejecuta sin
shell, con lista de argumentos, tiempo limite y flags `-O3 -ffp-contract=off
-fPIC -shared`: sin `-march=native` (la cache se puede copiar a otra CPU), sin
`-ffast-math` ni FMA (cambiarian los resultados segun la plataforma). La
biblioteca se guarda en `$WSBUILDER_CACHE/accel` (por defecto
`~/.cache/wsbuilder/accel`):

- El directorio y los padres que falten se crean con modo 0700. Se rechaza si
  es un enlace simbolico, no es del usuario o lo pueden escribir otros, o si un
  directorio superior es de otro usuario (salvo root) o escribible por otros sin
  el bit *sticky*. En ese caso `auto` usa `python`; elige otro directorio con
  `WSBUILDER_CACHE`.
- El nombre del archivo es el SHA-256 del fuente C, la plataforma, la version
  de Python y el comando del compilador; un archivo `.json` al lado guarda el
  SHA-256 de la biblioteca construida. Una biblioteca cuyo `.json` o cuyos
  bytes no coinciden se recompila y nunca se carga.
- Un candado de archivo (`flock`) serializa comprobacion, compilacion y carga
  entre procesos; la compilacion se mueve a su sitio con `os.replace` y se
  vuelve a verificar antes de cargarla.
- Las bibliotecas de versiones anteriores se quedan en el directorio: borralo
  cuando quieras, se reconstruye al primer uso (~1 s).
- El paquete sigue siendo una rueda pura (`py3-none-any`): el `.c` va como
  dato del paquete, sin extensiones compiladas al instalar.

**Cifras medidas** (Intel i7-1355U, 12 hilos, Python 3.14, con ruido de otros
procesos: rangos de varias ejecuciones). Matrices de Qwen3-0.6B: una capa
(q, k, v, o, gate, up y down: 15,7·10^6 multiplicaciones-suma) y la cabeza de
salida (151936x1024 = 155,6·10^6):

| Backend | Capa (solo matrices) | Cabeza | Matrices por token (28 capas + cabeza) |
| --- | --- | --- | --- |
| `python` | 197-293 ms (0,05-0,08 GMAC/s) | 2,0-2,6 s | 7,5-10,8 s |
| `process` (12 procesos) | 43-53 ms (0,3-0,36 GMAC/s) | 0,42-0,44 s | 1,6-1,9 s |
| `native`, 1 hilo, F32 | 6,7-6,9 ms (2,3 GMAC/s) | 69-71 ms | 0,26 s |
| `native`, 12 hilos, F32 | 1,9-2,8 ms (5,6-8,3 GMAC/s) | 18 ms (8,7 GMAC/s) | 0,07-0,10 s |
| `native`, 12 hilos, Q8_0 | 2,4-2,6 ms (6-6,5 GMAC/s) | 16 ms (9,9 GMAC/s) | 0,08-0,09 s |

Un bloque completo del tamano de Qwen3-0.6B (normas, RoPE, atencion y
residuales incluidos, que siguen en Python salvo la atencion) tarda 4-6 ms con
`native` entre 16 y 256 tokens de contexto, de modo que Qwen3-0.6B queda en
**~0,13-0,2 s por token** (5-7 tokens/s); con `process` unos 2-4 s y con
`python` unos 8-10 s. El prompt se procesa token a token con las mismas
matrices preparadas. Los hilos solo compensan en matrices grandes: cada hilo
recibe al menos 2^18 multiplicaciones-suma (`NATIVE_CHUNK_MACS`); `process`
deja en el proceso principal las matrices de menos de 2^18 (`PROCESS_MIN_MACS`),
donde la comunicacion costaria mas que el calculo, y escala poco (4-6x con 12
procesos) porque sigue calculando con Python.

### Memoria

La cache KV ocupa `capas x 2 x kv_heads x head_dim x 8` bytes por token
(~460 KB en Qwen3-0.6B, ~1,9 GB con 4096 tokens). Las matrices ocupan 4 bytes
por parametro con `python` y `process` (~2,4 GB para 0,6B, aunque el GGUF este
en Q8_0); con `native`, los bytes de su dtype (Q8_0 ~1,06 bytes por parametro,
F16/BF16 2, F32 4; los tensores con `encoding` json se guardan como F32 o F64).

## Leer pesos safetensors y GGUF

`wsbuilder.weights` lee los pesos publicados en los dos formatos mas comunes
sin dependencias: **safetensors** (Hugging Face, tambien repartido en varios
archivos con `model.safetensors.index.json`) y **GGUF** versiones 2 y 3
(llama.cpp). El archivo se abre con `mmap` de solo lectura y cada tensor se lee
cuando se pide.

```python
from wsbuilder import open_weights

with open_weights("qwen3-0.6b-q8_0.gguf") as weights:   # o .safetensors / .index.json
    weights.format, weights.architecture                 # "gguf", "qwen3"
    weights.metadata["qwen3.block_count"]
    weights.summary()                                    # JSON acotado: tensores, dtypes...
    embd = weights.tensor("token_embd.weight")
    embd.shape, embd.dtype                               # orden por filas, "Q8_0"
    embd.raw()                                           # memoryview de solo lectura, sin copia
    embd.to_array()                                      # array("f") descuantizado
```

- **Lectores.** `SafetensorsReader`, `ShardedSafetensors` y `GGUFReader`
  comparten interfaz: `tensor(nombre)`, `names()`, `in`, `len()`,
  `parameter_count`, `metadata`, `summary()` y `close()` (idempotente, tambien
  como `with`). `detect_weights_format(path)` devuelve `"safetensors"`,
  `"gguf"` o `None` leyendo solo la cabecera.
- **Tensores.** `WeightTensor` expone `name`, `shape` (orden por filas: en GGUF
  se invierten las dimensiones, que el formato guarda de la mas interna a la
  mas externa), `dtype`, `element_count`, `nbytes`, `raw()`, `to_array()`,
  `values()` y `rows()`.
- **Tipos.** Se convierten `F64`, `F32`, `F16`, `BF16` y los enteros (`I8` a
  `I64`, `U8` a `U64`, `BOOL`); de los tipos cuantizados de GGUF se
  descuantizan `Q8_0` y `Q4_0`. El resto (`Q4_K`, `Q6_K`, `F8_*`...) solo da
  sus bytes con `raw()`; `to_array()` lanza `NotImplementedError`.
- **Entrada no confiable.** Antes de devolver el lector se comprueban
  cabeceras, conteos (`max_tensors`, `max_kv`), longitudes de cadenas
  (`max_string_bytes`), formas, tamanos, alineacion, limites y solapes; un
  archivo invalido lanza `WeightsFormatError` (subclase de `ValueError`). En
  un indice se rechazan rutas absolutas y `..`.
- **Cierre.** Las vistas de `raw()` siguen siendo validas tras `close()`; el
  mapeo se libera al soltar la ultima (`view.release()` o `with`).
- **Resumen.** En `summary()` los arreglos largos (como el vocabulario de un
  GGUF) aparecen como `{"type": "array", "item_type": ..., "length": n}`.

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
- Se aceptan modelos NDJSON de wsbuilder y archivos de pesos safetensors y
  GGUF (formato `safetensors` o `gguf`; `store.open()` devuelve su lector y
  `store.load()` lanza `NotImplementedError`). Otros archivos se rechazan salvo
  que se pase `require_known_format=False` (quedan guardados con formato
  `unknown`, pero no se pueden abrir). Con la opcion por defecto un archivo
  malformado nunca entra al almacen; con `require_known_format=False` se guarda
  como `unknown`.
- `import_file(..., move=True)` mueve el archivo en vez de copiarlo. Si
  recibe un `model.json` o un directorio, lo importa como modelo de grafo
  (`import_graph`).

El resumen incluye `name`, `architecture`, `parameters`, `tensors`, `dtypes`
(valores por tipo), `metadata` y `file_size`.

### Modelos de grafo en el almacen

Un modelo de grafo (`model.json` mas los pesos y el tokenizador que referencia)
se guarda con formato `graph`, para cualquier arquitectura convertida:

```python
from wsbuilder import ModelStore

store = ModelStore()
record = store.convert("Qwen3-0.6B")             # directorio de Hugging Face o archivo .gguf
gguf = store.import_file("qwen3-0.6b-q8_0.gguf")
graph = store.convert(gguf.id, dtype="F16")      # un GGUF ya guardado, por su id
local = store.import_graph("qwen3-graph/")       # un model.json o su directorio
hub = store.download_huggingface_model("Qwen/Qwen3-0.6B")   # descarga, convierte y guarda

bot = store.load(record.id, accel="auto", max_context=2048, temperature=0.7)
print(bot.chat("Hola"))
store.export(record.id, "/backup/qwen3")         # copia el directorio completo
store.verify(record.id)                          # comprueba todos los archivos
```

- **Contenido.** El directorio del modelo tiene `model.json`, los archivos que
  referencia (con su nombre relativo) y `manifest.json`. `record.filename` es
  `model.json`, `record.size` la suma de todos los archivos y `record.sha256`
  el del `model.json`. El resumen tiene `architecture`, `name`, `config`,
  `generation`, `chat_template`, `parameters`, `tensors`, `dtypes`, `weights`,
  `tokenizer` y `files` (`{nombre: {"size", "sha256"}}` de cada archivo).
- **`import_graph(path, name=None, model_id=None, move=False)`.** Copia (o
  mueve) solo `model.json` y los archivos que referencia; rechaza referencias
  absolutas, con `..`, a nombres reservados (`model.json`, `manifest.json`),
  repetidas o que salen del directorio por un enlace simbolico. Valida la copia
  sin ejecutar el modelo: el `model.json`, cada registro de los pesos, que
  existan los tensores del grafo y que el tokenizador cargue. El directorio
  aparece en el almacen con un unico `os.replace` (manifiesto incluido); un
  fallo no deja nada en el almacen ni en `.staging`, y con `move=True`
  devuelve los archivos a su sitio.
- **`convert(source, name=None, model_id=None, dtype=None)`.** `source` es el
  id de un GGUF guardado o una ruta local (directorio de Hugging Face o
  `.gguf`); un texto que coincide con un id se toma como id. Un safetensors
  suelto del almacen no tiene `config.json` ni tokenizador: lanza
  `ValueError` (convierte su directorio de Hugging Face). Una arquitectura no
  soportada lanza el `NotImplementedError` del conversor. El origen queda como
  `convert:<id o ruta>`.
- **`download_huggingface_model(repo, revision="main", dtype=None, ...)`.**
  Descarga solo `config.json`, `tokenizer.json`, `generation_config.json` y
  `tokenizer_config.json` si existen, y `model.safetensors` o
  `model.safetensors.index.json` con sus shards (nombres simples
  `*.safetensors` que esten en el listado). Cada archivo se verifica con el
  listado del Hub: los de LFS por su SHA-256 y los demas por su id de objeto
  git (SHA-1 de `blob <tamano>\0` + contenido); sin ninguno de los dos se
  rechaza salvo `verify=False`. `max_bytes` acota el total y
  `progress(done, total)` cuenta bytes de todos los archivos. La descarga va a
  un directorio temporal del almacen que se borra al terminar o fallar; luego
  se convierte y se guarda con origen `hf:<repo>@<revision>`.
- **`load(id, accel=None, max_context=None, **opciones)`.** Usa
  `LanguageModel.from_graph`; `accel` y `max_context` van a `GraphModel` y el
  resto son opciones de generacion por defecto.
- **`open(id)`** devuelve el `ModelReader` de los pesos (no ejecuta el modelo).

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
- `allow_download=True` con `{"repo", "convert": true}` (sin `filename`)
  descarga el repositorio completo con `download_huggingface_model`, lo
  convierte y lo guarda como grafo; acepta `revision`, `name` y `dtype`.
- `allow_import=True` activa `POST /api/llm/import` (`{"path"}`; tambien un
  `model.json` o su directorio) y `POST /api/llm/convert`
  (`{"source", "name", "dtype"}`, con un id del almacen o una ruta del
  servidor), que corre como tarea consultable en `GET /api/llm/tasks?id=`.
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

messages = [{"role": "user", "content": "Hola"}]
ids = tok.encode_chat(messages, enable_thinking=False)   # seguro con texto no confiable
prompt = format_qwen3_chat(messages, enable_thinking=False)   # el mismo prompt como texto
text = tok.decode(ids, skip_special_tokens=True)

stream = tok.stream_decoder()             # para streaming: retiene UTF-8 incompleto
pieces = [stream.push(token_id) for token_id in ids]
```

- `encode(text, add_special_tokens=False, allow_special=True,
  split_added_tokens=True)` separa primero
  los tokens anadidos por coincidencia mas larga; con `allow_special=False` los
  especiales se tokenizan como texto normal. El valor por defecto
  (`allow_special=True`, como en Hugging Face) convierte un `<|im_end|>` escrito
  en el texto en un token de control: con entrada no confiable usa
  `encode_chat`, `allow_special=False` o `split_added_tokens=False` (este
  ultimo tampoco reconoce los tokens anadidos no especiales). `decode(ids,
  skip_special_tokens=False)` sustituye el UTF-8 invalido por U+FFFD.
- Normalizadores soportados: ninguno, `NFC`, `NFD`, `NFKC`, `NFKD` y `Sequence`
  de ellos. Pretokenizadores reconocidos: los patrones de Qwen2/Qwen3, Llama 3
  y GPT-2, implementados sin el modulo `regex`; con otro patron se lanza
  `ValueError`, salvo que se pase `pretokenizer=` (un nombre o una funcion).
- `to_json()` devuelve el `tokenizer.json` equivalente (ida y vuelta con
  `from_json`); los ids sin uso de un GGUF se guardan en la clave de extension
  `wsbuilder`.
- `from_gguf_metadata` no normaliza, como llama.cpp; pasa `normalizer="NFC"`
  para reproducir el tokenizador de Hugging Face de Qwen. Los tokens de tipo 5
  (sin uso, como `[PAD...]`) no se generan y se decodifican como texto vacio.
- `format_qwen3_chat(messages, add_generation_prompt=True,
  enable_thinking=True, system=None)` y `tok.encode_chat(...)` (mismos
  argumentos) generan el prompt ChatML de Qwen3 sin Jinja y quitan el bloque `<think>` de las respuestas anteriores a la ultima
  pregunta. `encode_chat` devuelve ids: `<|im_start|>`/`<|im_end|>` salen
  solo de la plantilla y el texto de los mensajes se codifica con
  `split_added_tokens=False`: ningun token anadido escrito en un mensaje
  (`<|im_end|>`, `<think>`, `<tool_call>`...) se convierte en su id, de modo
  que un usuario no puede inyectar un turno de sistema o de asistente. Exige
  `<|im_start|>`/`<|im_end|>` como tokens especiales. `system=` anade un
  primer mensaje de sistema (`ValueError` si los mensajes ya empiezan por uno). Los roles `tool` y las llamadas a
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
