# IA y Prediccion

## `DataSet`

`DataSet` es el contenedor basico para pares `X` y `Y`.

```python
from wsbuilder import DataSet

dataset = DataSet(
    [[1, 2], [2, 3], [3, 4], [4, 5]],
    [[0], [1], [1], [1]],
)
train, test = dataset.split(train_ratio=0.75, shuffle=False)
```

Metodos utiles:

- `sample_count`, `feature_count`, `target_count`
- `copy()`, `as_xy()`
- `shuffle()`, `split()`, `batches()`
- `describe_features()`, `describe_targets()`
- `standardize()`, `normalize_min_max()`

`split()` puede mezclar datos con `shuffle=True` y `seed` para resultados
repetibles. `batches()` permite entrenar por lotes.

## `NeuralNetwork`

La red neuronal es completamente Python puro.

```python
from wsbuilder import NeuralNetwork

net = NeuralNetwork(
    seed=7,
    learning_rate=0.3,
    loss="binary_cross_entropy",
    task="classification",
)
net.add_dense(6, input_size=2, activation="tanh")
net.add_dense(1, activation="sigmoid")
history = net.fit(
    [[0, 0], [0, 1], [1, 0], [1, 1]],
    [[0], [1], [1], [0]],
    epochs=5000,
    batch_size=4,
    shuffle=False,
)
```

Capacidades mas visibles:

- `fit` para regresion.
- `fit_classification` para etiquetas simbolicas.
- `predict`, `predict_batch`, `predict_proba`, `predict_class`.
- `evaluate`, `accuracy`, `classification_metrics`.
- `predict_with_metrics` para reportes de error y confianza.

Activaciones soportadas por la red:

- `linear`
- `sigmoid`
- `tanh`
- `relu`
- `leaky_relu`
- `softmax`

Losses soportadas:

- `mse`
- `binary_cross_entropy`
- `categorical_cross_entropy`

## `DenseLayer`

`DenseLayer` permite definir capas manuales y se usa como bloque interno del
`NeuralNetwork`.

Tambien puedes crear capas con `net.add_dense(units, input_size=..., activation=...)`,
que es la forma mas directa para usuarios de la libreria.

## Estadistica y error

Helpers exportados:

- `describe_data(values)`
- `evaluate_errors(expected, predicted, permissible_error=...)`

Devuelven `DataSummary` y `ErrorSummary`, ambos con la propiedad `uncertainty`.

## Entrenamiento en background

```python
from wsbuilder import NeuralNetwork, TaskManager, submit_training_task

X = [[0, 0], [0, 1], [1, 0], [1, 1]]
labels = ["no", "yes", "yes", "no"]

net = NeuralNetwork(
    seed=7,
    learning_rate=0.3,
    loss="binary_cross_entropy",
    task="classification",
)
net.add_dense(6, input_size=2, activation="tanh")
net.add_dense(1, activation="sigmoid")

tasks = TaskManager(max_concurrent=1)
task = submit_training_task(
    tasks,
    net,
    X,
    labels,
    classification=True,
    epochs=1000,
    batch_size=4,
    shuffle=False,
    name="ia-train",
)
history = task.get(timeout=5)
```

## Texto Unicode y tokens

`ByteTokenizer` representa texto como IDs de bytes UTF-8 entre 0 y 255. No
necesita vocabulario entrenado y conserva exactamente el texto valido, incluidos
acentos, arabe, chino, emoji y codigo fuente. No aplica normalizacion. Estos IDs
no son los tokens de un modelo externo ni una estimacion de su facturacion.

```python
from wsbuilder import ByteTokenizer

tokenizer = ByteTokenizer(max_bytes=1_000_000)
text = "Hola, 世界! مرحبا 👋\nprint('hola')"
tokens = tokenizer.encode(text)
assert tokenizer.decode(tokens) == text
assert tokenizer.vocab_size == 256
```

`encode` rechaza texto que exceda `max_bytes`; `decode` rechaza secuencias
demasiado largas, IDs no enteros o fuera de rango y bytes que no formen UTF-8
valido. Un token individual puede ser solo parte de un caracter multibyte:
decodifica secuencias completas. No pases IDs de longitud variable directamente
a una capa densa de dimension fija.

### Vectores de texto para la red

`HashingTextVectorizer` convierte ngramas de caracteres Unicode en vectores de
dimension fija usando un hash estable. Funciona tambien con idiomas que no
separan palabras con espacios; no necesita `fit` ni un vocabulario mutable.
Dos ngramas pueden caer en la misma posicion (colisiones). Estos vectores
describen patrones de caracteres, no son embeddings semanticos preentrenados.

```python
from wsbuilder import DataSet, HashingTextVectorizer, NeuralNetwork

vectorizer = HashingTextVectorizer(n_features=64, ngram_range=(1, 3))
texts = ["hola", "buen dia", "adios", "hasta luego"]
X = vectorizer.transform_batch(texts)
dataset = DataSet(X, [[1], [1], [0], [0]])

model = NeuralNetwork(
    seed=7, learning_rate=0.3,
    loss="binary_cross_entropy", task="classification",
)
model.add_dense(8, input_size=64, activation="tanh")
model.add_dense(1, activation="sigmoid")
model.fit_classification(
    dataset.X, ["saludo", "saludo", "despedida", "despedida"],
    epochs=300, batch_size=4, shuffle=False,
)
label = model.predict_class(vectorizer.transform("hola"))
```

Este conjunto diminuto ilustra la API; no demuestra comprension del lenguaje ni
generalizacion a otros idiomas. Conserva la misma configuracion del vectorizador
en entrenamiento e inferencia. El texto vacio produce un vector de ceros.
`max_chars` limita la longitud por texto (100 000 por defecto).
Por defecto se aplica `casefold` (`lowercase=True`), normalizacion Unicode NFC
y norma euclidea unitaria (`normalize=True`). NFC se aplica incluso con
`lowercase=False`; el tokenizador reversible no realiza estas transformaciones.
`n_features` admite 1 a 65 536 y los extremos de `ngram_range`, 1 a 8.
`transform_batch` no limita el numero de documentos: procesa tus colecciones
en lotes acotados.

## Audio WAV y caracteristicas

`load_wav` lee una ruta o un archivo binario seekable con WAV PCM entero de
8, 16, 24 o 32 bits. Convierte los canales a mono mediante su promedio: canales
en contrafase pueden cancelarse. No remuestrea ni decodifica MP3, AAC o audio
comprimido. No cierra el archivo proporcionado por el llamador.

```python
from wsbuilder import AudioData, NeuralNetwork, audio_features, load_wav

audio = load_wav("grabacion.wav", max_frames=1_000_000, max_channels=8)
features = audio_features(audio)
# Orden: duracion en segundos, media, RMS, pico absoluto, cruces por cero.
assert len(features) == 5

silence = AudioData(samples=(0.0,) * 8000, sample_rate=8000)
assert audio_features(silence) == [1.0, 0.0, 0.0, 0.0, 0.0]

audio_model = NeuralNetwork(seed=7)
audio_model.add_dense(1, input_size=5, activation="linear")
# Entrenar audio_model.fit(...) con ejemplos y objetivos propios antes de inferir.
```

Los limites de frames y canales se verifican antes de leer las muestras; los
archivos truncados se rechazan. Un frame contiene una muestra por canal, por lo
que un millon de frames equivale aproximadamente a 20,8 segundos a 48 kHz.
`AudioData` contiene una tupla de muestras mono finitas entre -1 y 1 y una
frecuencia entera en Hz positiva. `duration` devuelve la duracion en segundos;
una tupla vacia produce cinco caracteristicas cero. Los cruces por cero son
la fraccion de pares adyacentes que cambian de signo, considerando cero positivo.
Las caracteristicas resumen amplitud y duracion: no transcriben voz, reconocen
idiomas ni generan audio. Usa la misma frecuencia de muestreo para comparar
tasas de cruces por cero; divide los datos de entrenamiento y evaluacion por
grabacion para evitar evaluar sobre fragmentos del mismo audio.

Texto y audio pueden combinarse concatenando sus vectores y configurando
`input_size` con la suma de dimensiones. Entrena con ejemplos emparejados y
escalas consistentes; concatenar vectores no aporta por si solo comprension
multimodal. Los adaptadores no cambian las condiciones de concurrencia de
`NeuralNetwork`: evita compartir una instancia entre entrenamientos e
inferencias simultaneos.

## `Predictor`

`Predictor` es una utilidad matematica aparte para regresion lineal simple.

```python
from wsbuilder import Predictor

predictor = Predictor()
predictor.fit([[1], [2], [3], [4]], [[2], [4], [6], [8]])
prediction, sigma, lower, upper = predictor.predict([5])
```

Es util cuando quieres prediccion rapida sin configurar una red neuronal.

Despues de `fit`, la instancia expone informacion como `rank`, coeficientes y
residuos internos usados para calcular desviacion e intervalo estimado.
