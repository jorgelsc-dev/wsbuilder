"""IA y analitica basicas desde cero en Python puro.

Este modulo implementa:
- operaciones vectoriales y matriciales
- activaciones
- funciones de perdida
- capas densas
- red secuencial con backpropagation
- clasificacion y prediccion
- manejo de datos y estadistica descriptiva
- incertidumbre, desviacion y error maximo permisible

No usa numpy ni frameworks de ML.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import random
from statistics import NormalDist

__all__ = [
    "DataSet",
    "DataSummary",
    "DenseLayer",
    "ErrorSummary",
    "NeuralNetwork",
    "describe_data",
    "evaluate_errors",
    "submit_training_task",
]

_EPSILON = 1e-12
_DEFAULT_CONFIDENCE = 0.95

_ACTIVATION_ALIASES = {
    "identity": "linear",
    "none": "linear",
}

_LOSS_ALIASES = {
    "mean_squared_error": "mse",
    "mse_loss": "mse",
    "binary_crossentropy": "binary_cross_entropy",
    "categorical_crossentropy": "categorical_cross_entropy",
    "cross_entropy": "categorical_cross_entropy",
    "bce": "binary_cross_entropy",
    "cce": "categorical_cross_entropy",
}

_SUPPORTED_ACTIVATIONS = {
    "linear",
    "sigmoid",
    "tanh",
    "relu",
    "leaky_relu",
    "softmax",
}

_SUPPORTED_LOSSES = {
    "mse",
    "binary_cross_entropy",
    "categorical_cross_entropy",
}

_SUPPORTED_TASKS = {
    "regression",
    "classification",
}


def _normalize_name(value):
    if not isinstance(value, str):
        raise TypeError("name must be a string")
    return value.strip().lower().replace("-", "_").replace(" ", "_")


def _resolve_activation(name):
    normalized = _normalize_name(name)
    resolved = _ACTIVATION_ALIASES.get(normalized, normalized)
    if resolved not in _SUPPORTED_ACTIVATIONS:
        raise ValueError(
            f"Unsupported activation {name!r}. Supported: {sorted(_SUPPORTED_ACTIVATIONS)}"
        )
    return resolved


def _resolve_loss(name):
    normalized = _normalize_name(name)
    resolved = _LOSS_ALIASES.get(normalized, normalized)
    if resolved not in _SUPPORTED_LOSSES:
        raise ValueError(f"Unsupported loss {name!r}. Supported: {sorted(_SUPPORTED_LOSSES)}")
    return resolved


def _resolve_task(name):
    normalized = _normalize_name(name)
    if normalized not in _SUPPORTED_TASKS:
        raise ValueError(f"Unsupported task {name!r}. Supported: {sorted(_SUPPORTED_TASKS)}")
    return normalized


def _coverage_factor(confidence):
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be between 0 and 1")
    return NormalDist().inv_cdf((1.0 + confidence) / 2.0)


def _mean(values):
    if not values:
        raise ValueError("values must not be empty")
    return sum(values) / len(values)


def _variance(values, *, sample=True):
    if not values:
        raise ValueError("values must not be empty")
    if len(values) == 1:
        return 0.0
    mean_value = _mean(values)
    total = sum((value - mean_value) ** 2 for value in values)
    denominator = len(values) - 1 if sample else len(values)
    if denominator <= 0:
        return 0.0
    return total / denominator


def _std_dev(values, *, sample=True):
    return math.sqrt(_variance(values, sample=sample))


def _standard_error(values, *, sample=True):
    if not values:
        raise ValueError("values must not be empty")
    return _std_dev(values, sample=sample) / math.sqrt(len(values))


def _check_task_cancelled(task_handle):
    if task_handle is None:
        return
    if getattr(task_handle, "cancelled", False):
        from .tasks import TaskCancelledError

        raise TaskCancelledError("Training cancelled")


def _column_values(matrix, index):
    return [row[index] for row in matrix]


def _copy_matrix(matrix):
    return [row[:] for row in matrix]


def _clip(value, low, high):
    if value < low:
        return low
    if value > high:
        return high
    return value


def _vector_from_iterable(values, *, label, expected_length=None):
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{label} must be an iterable of numbers")
    try:
        vector = [float(value) for value in values]
    except TypeError as exc:
        raise TypeError(f"{label} must be an iterable of numbers") from exc
    except ValueError as exc:
        raise TypeError(f"{label} must contain numeric values") from exc
    if expected_length is not None and len(vector) != expected_length:
        raise ValueError(
            f"{label} must have length {expected_length}, got {len(vector)}"
        )
    return vector


def _matrix_from_iterable(rows, *, label):
    matrix = [_vector_from_iterable(row, label=f"{label}[{index}]") for index, row in enumerate(rows)]
    if not matrix:
        raise ValueError(f"{label} must not be empty")
    width = len(matrix[0])
    if width == 0:
        raise ValueError(f"{label} rows must not be empty")
    for row in matrix[1:]:
        if len(row) != width:
            raise ValueError(f"{label} rows must all have the same length")
    return matrix


def _zeros_vector(size):
    return [0.0 for _ in range(size)]


def _zeros_matrix(rows, cols):
    return [[0.0 for _ in range(cols)] for _ in range(rows)]


def _transpose(matrix):
    return [list(column) for column in zip(*matrix)]


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _matvec(matrix, vector):
    return [sum(value * vector[index] for index, value in enumerate(row)) for row in matrix]


def _outer(a, b):
    return [[x * y for y in b] for x in a]


def _add_inplace_vector(total, update):
    for index, value in enumerate(update):
        total[index] += value


def _add_inplace_matrix(total, update):
    for row_index, row in enumerate(update):
        target_row = total[row_index]
        for col_index, value in enumerate(row):
            target_row[col_index] += value


def _hadamard(a, b):
    return [x * y for x, y in zip(a, b)]


def _sigmoid(x):
    if x >= 0:
        exp_neg = math.exp(-x)
        return 1.0 / (1.0 + exp_neg)
    exp_pos = math.exp(x)
    return exp_pos / (1.0 + exp_pos)


def _softmax(values):
    peak = max(values)
    exps = [math.exp(value - peak) for value in values]
    total = sum(exps)
    if total == 0.0:
        return [1.0 / len(values) for _ in values]
    return [value / total for value in exps]


def _activate_vector(values, activation):
    if activation == "linear":
        return values[:]
    if activation == "sigmoid":
        return [_sigmoid(value) for value in values]
    if activation == "tanh":
        return [math.tanh(value) for value in values]
    if activation == "relu":
        return [value if value > 0.0 else 0.0 for value in values]
    if activation == "leaky_relu":
        return [value if value > 0.0 else 0.01 * value for value in values]
    if activation == "softmax":
        return _softmax(values)
    raise ValueError(f"Unsupported activation: {activation!r}")


def _activation_derivative(activation, z_values, output_values=None):
    if activation == "linear":
        return [1.0 for _ in z_values]
    if activation == "sigmoid":
        if output_values is None:
            output_values = [_sigmoid(value) for value in z_values]
        return [value * (1.0 - value) for value in output_values]
    if activation == "tanh":
        if output_values is None:
            output_values = [math.tanh(value) for value in z_values]
        return [1.0 - value * value for value in output_values]
    if activation == "relu":
        return [1.0 if value > 0.0 else 0.0 for value in z_values]
    if activation == "leaky_relu":
        return [1.0 if value > 0.0 else 0.01 for value in z_values]
    if activation == "softmax":
        raise ValueError("softmax derivative is only supported with categorical_cross_entropy")
    raise ValueError(f"Unsupported activation: {activation!r}")


def _loss_value(prediction, target, loss):
    if len(prediction) != len(target):
        raise ValueError("prediction and target must have the same length")
    if loss == "mse":
        size = max(1, len(prediction))
        return sum((pred - ref) ** 2 for pred, ref in zip(prediction, target)) / size
    if loss == "binary_cross_entropy":
        size = max(1, len(prediction))
        total = 0.0
        for pred, ref in zip(prediction, target):
            clipped = _clip(pred, _EPSILON, 1.0 - _EPSILON)
            total += -(ref * math.log(clipped) + (1.0 - ref) * math.log(1.0 - clipped))
        return total / size
    if loss == "categorical_cross_entropy":
        total = 0.0
        for pred, ref in zip(prediction, target):
            if ref != 0.0:
                total += -(ref * math.log(_clip(pred, _EPSILON, 1.0)))
        return total
    raise ValueError(f"Unsupported loss: {loss!r}")


@dataclass(frozen=True)
class DataSummary:
    """Resumen estadistico de una lista de valores numericos."""

    count: int
    minimum: float
    maximum: float
    mean: float
    variance: float
    std_dev: float
    standard_uncertainty: float
    coverage_factor: float
    expanded_uncertainty: float
    maximum_deviation: float
    confidence: float

    @property
    def uncertainty(self):
        return self.expanded_uncertainty


@dataclass(frozen=True)
class ErrorSummary:
    """Resumen estadistico de errores entre valores reales y predichos."""

    count: int
    mean_error: float
    mean_absolute_error: float
    mean_squared_error: float
    root_mean_squared_error: float
    variance: float
    std_dev: float
    standard_uncertainty: float
    coverage_factor: float
    expanded_uncertainty: float
    maximum_absolute_error: float
    maximum_permissible_error: float
    within_permissible_error: bool
    confidence: float

    @property
    def uncertainty(self):
        return self.expanded_uncertainty


def describe_data(values, *, confidence=_DEFAULT_CONFIDENCE):
    """Calcula estadistica descriptiva de una secuencia numerica."""
    vector = _vector_from_iterable(values, label="values")
    if not vector:
        raise ValueError("values must not be empty")
    mean_value = _mean(vector)
    variance_value = _variance(vector)
    std_value = math.sqrt(variance_value)
    standard_uncertainty = _standard_error(vector)
    coverage_factor = _coverage_factor(confidence)
    expanded_uncertainty = coverage_factor * standard_uncertainty
    maximum_deviation = max(abs(value - mean_value) for value in vector)
    return DataSummary(
        count=len(vector),
        minimum=min(vector),
        maximum=max(vector),
        mean=mean_value,
        variance=variance_value,
        std_dev=std_value,
        standard_uncertainty=standard_uncertainty,
        coverage_factor=coverage_factor,
        expanded_uncertainty=expanded_uncertainty,
        maximum_deviation=maximum_deviation,
        confidence=confidence,
    )


def evaluate_errors(
    actual,
    predicted,
    *,
    confidence=_DEFAULT_CONFIDENCE,
    permissible_error=None,
):
    """Calcula error medio, desviacion, incertidumbre y error maximo permisible."""
    actual_vector = _vector_from_iterable(actual, label="actual")
    predicted_vector = _vector_from_iterable(predicted, label="predicted")
    if len(actual_vector) != len(predicted_vector):
        raise ValueError("actual and predicted must have the same length")
    if not actual_vector:
        raise ValueError("actual and predicted must not be empty")

    errors = [predicted_value - actual_value for actual_value, predicted_value in zip(actual_vector, predicted_vector)]
    abs_errors = [abs(error) for error in errors]
    mean_error = _mean(errors)
    mean_absolute_error = _mean(abs_errors)
    mean_squared_error = _mean([error * error for error in errors])
    root_mean_squared_error = math.sqrt(mean_squared_error)
    variance_value = _variance(errors)
    std_value = math.sqrt(variance_value)
    standard_uncertainty = _standard_error(errors)
    coverage_factor = _coverage_factor(confidence)
    expanded_uncertainty = coverage_factor * standard_uncertainty
    maximum_absolute_error = max(abs_errors)
    if permissible_error is None:
        permissible_error = maximum_absolute_error
    within_permissible_error = maximum_absolute_error <= permissible_error
    return ErrorSummary(
        count=len(errors),
        mean_error=mean_error,
        mean_absolute_error=mean_absolute_error,
        mean_squared_error=mean_squared_error,
        root_mean_squared_error=root_mean_squared_error,
        variance=variance_value,
        std_dev=std_value,
        standard_uncertainty=standard_uncertainty,
        coverage_factor=coverage_factor,
        expanded_uncertainty=expanded_uncertainty,
        maximum_absolute_error=maximum_absolute_error,
        maximum_permissible_error=permissible_error,
        within_permissible_error=within_permissible_error,
        confidence=confidence,
    )


class DataSet:
    """Contenedor ligero para manejar datos de entrenamiento y analisis."""

    def __init__(self, X, Y=None):
        self.X = _matrix_from_iterable(X, label="X")
        self.Y = None if Y is None else _matrix_from_iterable(Y, label="Y")
        if self.Y is not None and len(self.X) != len(self.Y):
            raise ValueError("X and Y must have the same number of samples")

    def __len__(self):
        return len(self.X)

    @property
    def sample_count(self):
        return len(self.X)

    @property
    def feature_count(self):
        return len(self.X[0]) if self.X else 0

    @property
    def target_count(self):
        return len(self.Y[0]) if self.Y else 0

    def copy(self):
        return DataSet(_copy_matrix(self.X), None if self.Y is None else _copy_matrix(self.Y))

    def as_xy(self):
        return _copy_matrix(self.X), None if self.Y is None else _copy_matrix(self.Y)

    def shuffle(self, seed=None):
        rng = random.Random(seed)
        indices = list(range(self.sample_count))
        rng.shuffle(indices)
        X = [self.X[index][:] for index in indices]
        Y = None if self.Y is None else [self.Y[index][:] for index in indices]
        return DataSet(X, Y)

    def split(self, train_ratio=0.8, *, shuffle=True, seed=None):
        """Divide el conjunto en entrenamiento y prueba."""
        if not 0.0 < train_ratio < 1.0:
            raise ValueError("train_ratio must be between 0 and 1")
        if self.sample_count < 2:
            raise ValueError("split requires at least two samples")
        dataset = self.shuffle(seed=seed) if shuffle else self
        split_index = int(round(dataset.sample_count * train_ratio))
        split_index = max(1, min(dataset.sample_count - 1, split_index))
        train_X = dataset.X[:split_index]
        test_X = dataset.X[split_index:]
        if dataset.Y is None:
            return DataSet(train_X), DataSet(test_X)
        train_Y = dataset.Y[:split_index]
        test_Y = dataset.Y[split_index:]
        return DataSet(train_X, train_Y), DataSet(test_X, test_Y)

    def batches(self, batch_size, *, shuffle=True, seed=None):
        """Genera mini-lotes como nuevos objetos DataSet."""
        if batch_size <= 0:
            raise ValueError("batch_size must be a positive integer")
        indices = list(range(self.sample_count))
        if shuffle:
            random.Random(seed).shuffle(indices)
        for start in range(0, self.sample_count, batch_size):
            batch_indices = indices[start : start + batch_size]
            X = [self.X[index][:] for index in batch_indices]
            Y = None if self.Y is None else [self.Y[index][:] for index in batch_indices]
            yield DataSet(X, Y)

    def describe_features(self, *, confidence=_DEFAULT_CONFIDENCE):
        """Calcula estadistica por columna de las entradas."""
        return [describe_data(_column_values(self.X, index), confidence=confidence) for index in range(self.feature_count)]

    def describe_targets(self, *, confidence=_DEFAULT_CONFIDENCE):
        """Calcula estadistica por columna de las salidas."""
        if self.Y is None:
            raise ValueError("This dataset does not contain targets")
        return [describe_data(_column_values(self.Y, index), confidence=confidence) for index in range(self.target_count)]

    def standardize(self, *, epsilon=1e-12):
        """Devuelve una copia estandarizada y los parametros usados."""
        feature_stats = self.describe_features()
        new_X = []
        for row in self.X:
            normalized = []
            for index, value in enumerate(row):
                std = feature_stats[index].std_dev
                normalized.append((value - feature_stats[index].mean) / (std if std > epsilon else 1.0))
            new_X.append(normalized)
        return DataSet(new_X, None if self.Y is None else _copy_matrix(self.Y)), feature_stats

    def normalize_min_max(self, *, low=0.0, high=1.0):
        """Escala las columnas a un rango dado."""
        if high <= low:
            raise ValueError("high must be greater than low")
        feature_stats = self.describe_features()
        new_X = []
        for row in self.X:
            normalized = []
            for index, value in enumerate(row):
                minimum = feature_stats[index].minimum
                maximum = feature_stats[index].maximum
                if maximum == minimum:
                    normalized.append(low)
                else:
                    normalized.append(low + (value - minimum) * (high - low) / (maximum - minimum))
            new_X.append(normalized)
        return DataSet(new_X, None if self.Y is None else _copy_matrix(self.Y)), feature_stats

    def __repr__(self):
        return f"DataSet(samples={self.sample_count}, features={self.feature_count}, targets={self.target_count})"


class DenseLayer:
    """Capa densa totalmente conectada."""

    def __init__(self, input_size, output_size, activation="linear", *, rng=None, seed=None):
        if input_size <= 0 or output_size <= 0:
            raise ValueError("input_size and output_size must be positive integers")
        self.input_size = int(input_size)
        self.output_size = int(output_size)
        self.activation = _resolve_activation(activation)
        self._rng = rng if rng is not None else random.Random(seed)

        if self.activation in {"relu", "leaky_relu"}:
            limit = math.sqrt(2.0 / self.input_size)
        else:
            limit = math.sqrt(6.0 / (self.input_size + self.output_size))

        self.weights = [
            [self._rng.uniform(-limit, limit) for _ in range(self.input_size)]
            for _ in range(self.output_size)
        ]
        self.biases = [0.0 for _ in range(self.output_size)]
        self._cache_input = None
        self._cache_z = None
        self._cache_output = None

    def __repr__(self):
        return (
            f"DenseLayer(input_size={self.input_size}, output_size={self.output_size}, "
            f"activation={self.activation!r})"
        )

    def forward(self, inputs):
        vector = _vector_from_iterable(inputs, label="inputs", expected_length=self.input_size)
        self._cache_input = vector
        z_values = [
            _dot(row, vector) + bias for row, bias in zip(self.weights, self.biases)
        ]
        self._cache_z = z_values
        outputs = _activate_vector(z_values, self.activation)
        self._cache_output = outputs
        return outputs[:]

    def gradients(self, grad_z):
        if self._cache_input is None or self._cache_z is None:
            raise RuntimeError("forward() must be called before gradients()")
        grad_vector = _vector_from_iterable(grad_z, label="grad_z", expected_length=self.output_size)
        grad_weights = _outer(grad_vector, self._cache_input)
        grad_biases = grad_vector[:]
        grad_input = _matvec(_transpose(self.weights), grad_vector)
        return grad_input, grad_weights, grad_biases

    def apply_gradients(self, grad_weights, grad_biases, scale):
        if len(grad_weights) != self.output_size or len(grad_biases) != self.output_size:
            raise ValueError("gradient shapes do not match the layer shape")
        for row_index in range(self.output_size):
            weight_row = self.weights[row_index]
            grad_row = grad_weights[row_index]
            for col_index in range(self.input_size):
                weight_row[col_index] -= scale * grad_row[col_index]
            self.biases[row_index] -= scale * grad_biases[row_index]


class NeuralNetwork:
    """Red secuencial de capas densas entrenada con backpropagation."""

    def __init__(self, *, seed=None, learning_rate=0.1, loss="mse", task="regression", classification_threshold=0.5):
        self._rng = random.Random(seed)
        self.learning_rate = float(learning_rate)
        self.loss = _resolve_loss(loss)
        self.task = _resolve_task(task)
        self.classification_threshold = float(classification_threshold)
        self.layers = []
        self._class_labels = None

    def __repr__(self):
        return (
            f"NeuralNetwork(layers={len(self.layers)}, loss={self.loss!r}, "
            f"task={self.task!r}, learning_rate={self.learning_rate})"
        )

    @property
    def input_size(self):
        if not self.layers:
            return None
        return self.layers[0].input_size

    @property
    def output_size(self):
        if not self.layers:
            return None
        return self.layers[-1].output_size

    def summary(self):
        lines = [
            f"NeuralNetwork(loss={self.loss!r}, task={self.task!r}, learning_rate={self.learning_rate})",
            f"layers={len(self.layers)}",
        ]
        for index, layer in enumerate(self.layers):
            lines.append(
                f"  {index}: Dense({layer.input_size} -> {layer.output_size}, activation={layer.activation})"
            )
        return "\n".join(lines)

    def add_dense(self, units, input_size=None, activation="linear"):
        if units <= 0:
            raise ValueError("units must be a positive integer")
        if not self.layers and input_size is None:
            raise ValueError("input_size is required for the first layer")
        if self.layers:
            expected = self.layers[-1].output_size
            if input_size is None:
                input_size = expected
            elif int(input_size) != expected:
                raise ValueError(f"input_size must be {expected} for this layer")
        layer = DenseLayer(
            input_size=input_size,
            output_size=units,
            activation=activation,
            rng=self._rng,
        )
        self.layers.append(layer)
        return layer

    def _coerce_dataset(self, X, Y=None):
        if Y is None and isinstance(X, DataSet):
            if X.Y is None:
                raise ValueError("The dataset does not contain targets")
            return X.X, X.Y
        if Y is None:
            raise TypeError("Y is required unless X is a DataSet with targets")
        return X, Y

    def forward(self, inputs):
        if not self.layers:
            raise RuntimeError("The network has no layers")
        vector = _vector_from_iterable(inputs, label="inputs", expected_length=self.layers[0].input_size)
        for layer in self.layers:
            vector = layer.forward(vector)
        return vector[:]

    def predict(self, inputs):
        return self.forward(inputs)

    def predict_proba(self, inputs):
        return self.predict(inputs)

    def predict_class(self, inputs, *, threshold=None):
        probabilities = self.predict_proba(inputs)
        if not probabilities:
            raise RuntimeError("The network produced no outputs")
        if threshold is None:
            threshold = self.classification_threshold
        if len(probabilities) == 1:
            label_index = 1 if probabilities[0] >= threshold else 0
        else:
            label_index = max(range(len(probabilities)), key=lambda index: probabilities[index])
        if self._class_labels is not None and len(self._class_labels) > label_index:
            return self._class_labels[label_index]
        return label_index

    def predict_batch(self, samples):
        return [self.predict(sample) for sample in samples]

    def _validate_training_setup(self, X, Y):
        X = X.X if isinstance(X, DataSet) else X
        Y = Y.Y if isinstance(Y, DataSet) else Y
        if not self.layers:
            raise RuntimeError("The network has no layers")
        X_rows = _matrix_from_iterable(X, label="X")
        Y_rows = _matrix_from_iterable(Y, label="Y")
        if len(X_rows) != len(Y_rows):
            raise ValueError("X and Y must have the same number of samples")
        if len(X_rows[0]) != self.layers[0].input_size:
            raise ValueError(
                f"X samples must have length {self.layers[0].input_size}"
            )
        if len(Y_rows[0]) != self.layers[-1].output_size:
            raise ValueError(
                f"Y samples must have length {self.layers[-1].output_size}"
            )
        for layer in self.layers[:-1]:
            if layer.activation == "softmax":
                raise ValueError("softmax is only supported on the output layer")
        if self.loss == "binary_cross_entropy" and self.layers[-1].activation != "sigmoid":
            raise ValueError("binary_cross_entropy requires a sigmoid output layer")
        if self.loss == "categorical_cross_entropy" and self.layers[-1].activation != "softmax":
            raise ValueError("categorical_cross_entropy requires a softmax output layer")
        return X_rows, Y_rows

    def _output_gradient(self, prediction, target):
        size = max(1, len(prediction))
        if self.loss == "mse":
            return [2.0 * (pred - ref) / size for pred, ref in zip(prediction, target)]
        if self.loss == "binary_cross_entropy":
            return [(pred - ref) / size for pred, ref in zip(prediction, target)]
        if self.loss == "categorical_cross_entropy":
            return [pred - ref for pred, ref in zip(prediction, target)]
        raise ValueError(f"Unsupported loss: {self.loss!r}")

    def evaluate(self, X, Y):
        X, Y = self._coerce_dataset(X, Y)
        X_rows, Y_rows = self._validate_training_setup(X, Y)
        total = 0.0
        for inputs, target in zip(X_rows, Y_rows):
            prediction = self.predict(inputs)
            total += _loss_value(prediction, target, self.loss)
        return total / len(X_rows)

    def fit(self, X, Y, *, epochs=1000, learning_rate=None, batch_size=None, shuffle=True, task_handle=None):
        """Entrena la red para regresion o salidas numericas."""
        X, Y = self._coerce_dataset(X, Y)
        if epochs <= 0:
            raise ValueError("epochs must be a positive integer")
        if learning_rate is None:
            learning_rate = self.learning_rate
        learning_rate = float(learning_rate)
        if learning_rate <= 0:
            raise ValueError("learning_rate must be positive")
        X_rows, Y_rows = self._validate_training_setup(X, Y)
        sample_count = len(X_rows)
        if batch_size is None:
            batch_size = sample_count
        if batch_size <= 0:
            raise ValueError("batch_size must be a positive integer")
        batch_size = min(int(batch_size), sample_count)

        history = {"loss": []}
        indices = list(range(sample_count))

        for _epoch in range(epochs):
            _check_task_cancelled(task_handle)
            if shuffle and sample_count > 1:
                self._rng.shuffle(indices)
            epoch_loss = 0.0

            for batch_start in range(0, sample_count, batch_size):
                _check_task_cancelled(task_handle)
                batch_indices = indices[batch_start : batch_start + batch_size]
                batch_weights = [
                    _zeros_matrix(layer.output_size, layer.input_size) for layer in self.layers
                ]
                batch_biases = [_zeros_vector(layer.output_size) for layer in self.layers]

                for sample_index in batch_indices:
                    prediction = self.forward(X_rows[sample_index])
                    target = Y_rows[sample_index]
                    epoch_loss += _loss_value(prediction, target, self.loss)
                    grad = self._output_gradient(prediction, target)

                    for layer_index in range(len(self.layers) - 1, -1, -1):
                        layer = self.layers[layer_index]
                        grad_input, grad_weights, grad_biases = layer.gradients(grad)
                        _add_inplace_matrix(batch_weights[layer_index], grad_weights)
                        _add_inplace_vector(batch_biases[layer_index], grad_biases)
                        if layer_index > 0:
                            previous = self.layers[layer_index - 1]
                            grad = _hadamard(
                                grad_input,
                                _activation_derivative(
                                    previous.activation,
                                    previous._cache_z,
                                    previous._cache_output,
                                ),
                            )

                scale = learning_rate / len(batch_indices)
                for layer_index, layer in enumerate(self.layers):
                    layer.apply_gradients(batch_weights[layer_index], batch_biases[layer_index], scale)

                _check_task_cancelled(task_handle)

            history["loss"].append(epoch_loss / sample_count)

        self._update_calibration(X_rows, Y_rows)
        return history

    def fit_classification(self, X, labels, *, classes=None, epochs=1000, learning_rate=None, batch_size=None, shuffle=True, task_handle=None):
        """Entrena la red para clasificacion binaria o multiclase."""
        X_rows = _matrix_from_iterable(X.X if isinstance(X, DataSet) else X, label="X")
        labels = list(labels)
        if len(X_rows) != len(labels):
            raise ValueError("X and labels must have the same number of samples")
        if classes is None:
            classes = []
            for label in labels:
                if label not in classes:
                    classes.append(label)
        else:
            classes = list(classes)
        if len(classes) < 2:
            raise ValueError("classification requires at least two classes")
        class_to_index = {label: index for index, label in enumerate(classes)}
        if len(class_to_index) != len(classes):
            raise ValueError("classes must be unique")
        self.task = "classification"
        self._class_labels = classes

        if self.output_size == 1:
            if len(classes) != 2:
                raise ValueError("binary classification with one output requires exactly two classes")
            positive_class = classes[1]
            Y_rows = [[1.0 if label == positive_class else 0.0] for label in labels]
        else:
            if self.output_size != len(classes):
                raise ValueError("output layer size must match the number of classes")
            Y_rows = []
            for label in labels:
                row = [0.0 for _ in classes]
                row[class_to_index[label]] = 1.0
                Y_rows.append(row)

        return self.fit(
            X_rows,
            Y_rows,
            epochs=epochs,
            learning_rate=learning_rate,
            batch_size=batch_size,
            shuffle=shuffle,
            task_handle=task_handle,
        )

    def _update_calibration(self, X_rows, Y_rows):
        self._calibration = []
        for output_index in range(self.output_size):
            actual = [row[output_index] for row in Y_rows]
            predicted = [self.predict(row)[output_index] for row in X_rows]
            self._calibration.append(evaluate_errors(actual, predicted))

    def calibration(self):
        """Devuelve los errores y la incertidumbre calculada tras el entrenamiento."""
        if not hasattr(self, "_calibration"):
            raise RuntimeError("The network has not been calibrated yet")
        return list(self._calibration)

    def predict_with_metrics(self, inputs, *, expected=None, permissible_error=None, confidence=_DEFAULT_CONFIDENCE):
        """Devuelve prediccion, desviacion, incertidumbre y error maximo permisible."""
        prediction = self.predict(inputs)
        if not hasattr(self, "_calibration"):
            self._calibration = [
                ErrorSummary(
                    count=0,
                    mean_error=0.0,
                    mean_absolute_error=0.0,
                    mean_squared_error=0.0,
                    root_mean_squared_error=0.0,
                    variance=0.0,
                    std_dev=0.0,
                    standard_uncertainty=0.0,
                    coverage_factor=_coverage_factor(confidence),
                    expanded_uncertainty=0.0,
                    maximum_absolute_error=0.0,
                    maximum_permissible_error=0.0 if permissible_error is None else permissible_error,
                    within_permissible_error=True,
                    confidence=confidence,
                )
                for _ in prediction
            ]
        expected_vector = None
        if expected is not None:
            if len(prediction) == 1 and not isinstance(expected, (list, tuple, DataSet)):
                expected_vector = [float(expected)]
            else:
                expected_vector = _vector_from_iterable(expected, label="expected", expected_length=len(prediction))
        metrics = []
        for output_index, value in enumerate(prediction):
            calibration = self._calibration[min(output_index, len(self._calibration) - 1)]
            item = {
                "prediction": value,
                "deviation": calibration.std_dev,
                "uncertainty": calibration.uncertainty,
                "standard_uncertainty": calibration.standard_uncertainty,
                "maximum_permissible_error": calibration.maximum_permissible_error,
            }
            if expected_vector is not None:
                actual_error = value - expected_vector[output_index]
                item["actual_error"] = actual_error
                item["within_permissible_error"] = abs(actual_error) <= item["maximum_permissible_error"]
            metrics.append(item)
        if len(prediction) == 1:
            metrics = metrics[0]
        return {
            "prediction": prediction,
            "metrics": metrics,
        }

    def accuracy(self, X, labels):
        """Calcula exactitud de clasificacion."""
        if self.task != "classification":
            raise RuntimeError("accuracy is only available for classification tasks")
        X_rows = _matrix_from_iterable(X.X if isinstance(X, DataSet) else X, label="X")
        labels = list(labels)
        if len(X_rows) != len(labels):
            raise ValueError("X and labels must have the same number of samples")
        correct = 0
        for inputs, label in zip(X_rows, labels):
            if self.predict_class(inputs) == label:
                correct += 1
        return correct / len(labels)

    def classification_metrics(self, X, labels):
        X_rows = _matrix_from_iterable(X.X if isinstance(X, DataSet) else X, label="X")
        labels = list(labels)
        if len(X_rows) != len(labels):
            raise ValueError("X and labels must have the same number of samples")
        predictions = [self.predict_class(row) for row in X_rows]
        accuracy_value = sum(pred == label for pred, label in zip(predictions, labels)) / len(labels)
        return {
            "total": len(labels),
            "correct": sum(pred == label for pred, label in zip(predictions, labels)),
            "accuracy": accuracy_value,
            "predictions": predictions,
        }


def submit_training_task(
    task_manager,
    network,
    X,
    Y=None,
    *,
    classification=False,
    classes=None,
    name="ia-train",
    group="ia",
    metadata=None,
    **fit_kwargs,
):
    """Ejecuta el entrenamiento de una red en background usando TaskManager.

    Parameters
    ----------
    task_manager:
        Instancia con metodo ``spawn`` compatible con ``wsbuilder.tasks.TaskManager``.
    network:
        Instancia de :class:`NeuralNetwork`.
    X, Y:
        Datos de entrenamiento. Si ``classification`` es True, ``Y`` debe contener
        las etiquetas originales.
    classes:
        Etiquetas ordenadas para clasificacion multiclase o binaria.
    fit_kwargs:
        Argumentos adicionales para ``fit`` o ``fit_classification``.
    """
    if task_manager is None or not hasattr(task_manager, "spawn"):
        raise TypeError("task_manager must provide a spawn() method")

    task_metadata = dict(metadata or {})
    task_metadata.setdefault("module", "wsbuilder.ia")
    task_metadata.setdefault("classification", bool(classification))
    task_metadata.setdefault("network_task", getattr(network, "task", "regression"))

    def _runner(task_handle):
        if classification:
            return network.fit_classification(
                X,
                Y,
                classes=classes,
                task_handle=task_handle,
                **fit_kwargs,
            )
        return network.fit(X, Y, task_handle=task_handle, **fit_kwargs)

    return task_manager.spawn(
        _runner,
        name=name,
        group=group,
        metadata=task_metadata,
        pass_handle=True,
    )
