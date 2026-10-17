# IA

WSBuilder incluye herramientas sencillas de data science y redes neuronales hechas desde cero.

## Bloques

- `DataSet`
- `DataSummary`
- `ErrorSummary`
- `DenseLayer`
- `NeuralNetwork`
- `Predictor`
- `describe_data`
- `evaluate_errors`
- `submit_training_task`

## Datos

```python
from wsbuilder import DataSet, describe_data, evaluate_errors

dataset = DataSet([[1, 2], [2, 3], [3, 4]], [[0], [1], [1]])
train, test = dataset.split(train_ratio=0.67, shuffle=True, seed=42)
summary = describe_data([1.0, 1.2, 0.9, 1.1])
errors = evaluate_errors([10.0, 11.0], [9.8, 11.4], permissible_error=0.5)
```

## Red neuronal

```python
from wsbuilder import NeuralNetwork

net = NeuralNetwork(seed=7, learning_rate=0.3, loss="binary_cross_entropy", task="classification")
net.add_dense(6, input_size=2, activation="tanh")
net.add_dense(1, activation="sigmoid")
```

Entrenamiento:

```python
history = net.fit_classification(
    [[0, 0], [0, 1], [1, 0], [1, 1]],
    ["no", "yes", "yes", "no"],
    epochs=3000,
    batch_size=4,
    shuffle=False,
)
```

## Prediccion simple

`Predictor` ofrece una alternativa aun mas ligera para regresion lineal basica:

```python
from wsbuilder import Predictor

model = Predictor()
model.fit([[1, 2], [2, 3], [3, 4]], [[2], [3], [4]])
pred, std, lim_inf, lim_sup = model.predict([4, 5])
```

## Trabajo en background

```python
from wsbuilder import TaskManager, submit_training_task

tasks = TaskManager(max_concurrent=1)
task = submit_training_task(tasks, net, [[0, 0], [0, 1]], ["no", "yes"], classification=True)
history = task.get(timeout=10)
```

## Recomendacion

Si quieres usar estas piezas en serio, manten el entrenamiento pequeno y determinista. Para uso mas amplio,
el modulo sirve mejor como base educativa o prototipo rapido que como reemplazo de librerias especializadas.
