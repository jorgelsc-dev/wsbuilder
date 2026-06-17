# wsbuilder

`wsbuilder` es una libreria Python para construir servidores HTTP, WebSocket y utilidades de infraestructura en un unico paquete.

Se apoya en la biblioteca estandar y expone bloques pequenos y composables para:

- routing HTTP y respuestas tipadas.
- WebSocket de bajo nivel con control de frames.
- ORM ligero para SQLite.
- cache, seguridad, metricas y tareas en background.
- DNS local y replicas SQLite optimizadas.

## Mapa rapido

<div class="diagram">
<div class="diagram-title">Mapa rapido</div>
<div class="diagram-track">
<div class="diagram-node">Cliente</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">HTTPServer</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">App</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">Router</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">HTTP / WS</div>
</div>
<div class="diagram-note" style="margin-top: 0.85rem;">El servicio se organiza alrededor de `App` y sus capas transversales.</div>
</div>

## Por que destaca

- Menos superficie de dependencia en runtime.
- Flujo explicito: request, dispatch, respuesta y cierre.
- Modulos separados que puedes activar solo cuando los necesitas.
- API publica uniforme: `App`, `Response`, `Database`, `TaskManager`, `LocalDNSServer`.
- Adecuado para servicios pequenos, medianos y capas de borde en Microservicios.

## Instalacion

```bash
python -m pip install -e .
```

## Inicio rapido

```python
from wsbuilder import App, Response

app = App(cors_allow_origin="*")
app.enable_metrics()

@app.view("/")
def home(_request):
    return Response.html("<h1>wsbuilder</h1>")

@app.api("/api/health")
def health(_request):
    return {"ok": True}

app.run("0.0.0.0", 8765)
```

## Mapa de la libreria

<div class="diagram">
<div class="diagram-title">Mapa de la libreria</div>
<div class="diagram-track">
<div class="diagram-node">framework / App</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">http / ws / orm</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">cache / security</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">metrics / tasks</div>
<div class="diagram-arrow">→</div>
<div class="diagram-node">dns / replicas / utils</div>
</div>
</div>

- `wsbuilder.framework`: fachada publica con `App`, `Request`, `Response`, `HTTPServer`, `WebSocket` y helpers.
- `wsbuilder.http`: parseo HTTP, request/response y streaming.
- `wsbuilder.ws`: handshake, frames y errores WebSocket.
- `wsbuilder.orm`: modelos SQLite, `QuerySet`, transacciones y helpers SQL.
- `wsbuilder.cache` y `wsbuilder.caches`: cache en memoria y cache declarativa de respuestas.
- `wsbuilder.security`: ACL, rate limiting y decision engine.
- `wsbuilder.metrics`: snapshot y stream de observabilidad.
- `wsbuilder.tasks`: trabajo asincrono controlado por `TaskManager`.
- `wsbuilder.dns`: servidor DNS UDP local.
- `wsbuilder.db_replicas`: lectura optimizada y pool de replicas SQLite.
- `wsbuilder.cookies` y `wsbuilder.headers`: utilidades HTTP de bajo nivel.
- `wsbuilder.ia`: redes neuronales basicas desde cero, con clasificacion, prediccion y estadistica.
- `wsbuilder.predicts`: utilidad matematica `Predictor`.

## Casos de uso

1. APIs REST pequenas con respuestas JSON y HTML.
2. Chat, notificaciones y telemetria sobre WebSocket.
3. Persistencia local con SQLite y modelo declarativo.
4. Cache de rutas o contenido calculado.
5. Control de acceso, bloqueo temporal y observabilidad interna.
6. Procesos de background y lectura optimizada sobre SQLite.

## IA desde cero

`wsbuilder.ia` incluye tres bloques:

- `DataSet` para manejar datos, dividir conjuntos, mezclar y calcular estadistica por columna.
- `NeuralNetwork` y `DenseLayer` para regresion y clasificacion.
- `describe_data` y `evaluate_errors` para desviacion, incertidumbre y error maximo permisible.

### Regresion

```python
from wsbuilder import NeuralNetwork

X = [[0.0], [1.0], [2.0], [3.0]]
Y = [[0.0], [1.0], [2.0], [3.0]]

net = NeuralNetwork(seed=7, learning_rate=0.1, loss="mse")
net.add_dense(4, input_size=1, activation="tanh")
net.add_dense(1, activation="linear")

net.fit(X, Y, epochs=2000, batch_size=4)

prediction = net.predict([1.5])
report = net.predict_with_metrics([1.5], expected=[1.5], permissible_error=0.25)
```

### Clasificacion

```python
from wsbuilder import NeuralNetwork

X = [[0, 0], [0, 1], [1, 0], [1, 1]]
labels = ["no", "yes", "yes", "yes"]

clf = NeuralNetwork(seed=3, learning_rate=0.3, loss="binary_cross_entropy", task="classification")
clf.add_dense(6, input_size=2, activation="tanh")
clf.add_dense(1, activation="sigmoid")

clf.fit_classification(X, labels, epochs=3000, batch_size=4)
label = clf.predict_class([1, 0])
probability = clf.predict_proba([1, 0])
accuracy = clf.accuracy(X, labels)
```

### Manejo de datos

```python
from wsbuilder.ia import DataSet, describe_data, evaluate_errors

dataset = DataSet([[1, 2], [2, 3], [3, 4]], [[0], [1], [1]])
train, test = dataset.split(train_ratio=0.67, shuffle=True, seed=42)
stats = dataset.describe_features()
errors = evaluate_errors([10.0, 11.0], [9.8, 11.4], permissible_error=0.5)
summary = describe_data([1.0, 1.2, 0.9, 1.1])
```

### Entrenamiento en background con Tasks

```python
from wsbuilder import NeuralNetwork, TaskManager, submit_training_task

X = [[0, 0], [0, 1], [1, 0], [1, 1]]
labels = ["no", "yes", "yes", "yes"]

clf = NeuralNetwork(seed=3, learning_rate=0.3, loss="binary_cross_entropy", task="classification")
clf.add_dense(6, input_size=2, activation="tanh")
clf.add_dense(1, activation="sigmoid")

tasks = TaskManager(max_concurrent=1)
task = submit_training_task(
    tasks,
    clf,
    X,
    labels,
    classification=True,
    epochs=3000,
    batch_size=4,
    shuffle=False,
)

history = task.get()
label = clf.predict_class([1, 0])
```

## Documentacion

- [Inicio](docs/index.md)
- [Arquitectura](docs/architecture.md)
- [Ayuda](docs/help/index.md)
- [Referencia](docs/reference/index.md)

## Contribucion y soporte

- Crea ramas desde `main` usando `feat/<nombre>` o `fix/<nombre>`.
- Mantiene los cambios enfocados en un solo tema por PR.
- Abre el pull request hacia `main` con una descripcion corta y notas de riesgo.
- Si encuentras un problema de seguridad, reportalo de forma privada.

### Soporte opcional

Si `wsbuilder` te resulta util y quieres apoyar el mantenimiento del proyecto, puedes donar en BTC:

`bc1q3lhxpr9yantvefmvhpd2h4lu0ykf3t45zvuve2`

Red: `BTC mainnet` (Native SegWit).
