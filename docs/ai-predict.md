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

- `sample_count()`, `feature_count()`, `target_count()`
- `copy()`, `as_xy()`
- `shuffle()`, `split()`, `batches()`
- `describe_features()`, `describe_targets()`
- `standardize()`, `normalize_min_max()`

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

## `DenseLayer`

`DenseLayer` permite definir capas manuales y se usa como bloque interno del
`NeuralNetwork`.

## Estadistica y error

Helpers exportados:

- `describe_data(values)`
- `evaluate_errors(expected, predicted, permissible_error=...)`

Devuelven `DataSummary` y `ErrorSummary`, ambos con `uncertainty()`.

## Entrenamiento en background

```python
from wsbuilder import TaskManager, submit_training_task

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

## `Predictor`

`Predictor` es una utilidad matematica aparte para regresion lineal simple.

```python
from wsbuilder import Predictor

predictor = Predictor()
predictor.fit([[1], [2], [3], [4]], [[2], [4], [6], [8]])
prediction, sigma, lower, upper = predictor.predict([5])
```

Es util cuando quieres prediccion rapida sin configurar una red neuronal.
