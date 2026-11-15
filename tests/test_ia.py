import random
import unittest
from statistics import NormalDist

from wsbuilder import (
    DataSet,
    NeuralNetwork,
    TaskManager,
    describe_data,
    evaluate_errors,
    submit_training_task,
)
from wsbuilder.ia import (
    DenseLayer,
    _activate_vector,
    _activation_backward,
    _activation_derivative,
    _hadamard,
    _loss_value,
)


def _reference_gradients(layer, grad_z):
    """Gradientes con las formulas originales (producto exterior y traspuesta)."""
    grad = [float(value) for value in grad_z]
    inputs = layer._cache_input
    grad_weights = [[x * y for y in inputs] for x in grad]
    transposed = [list(column) for column in zip(*layer.weights)]
    grad_input = [
        sum(value * grad[index] for index, value in enumerate(row)) for row in transposed
    ]
    return grad_input, grad_weights, grad[:]


def _reference_forward(net, inputs):
    vector = [float(value) for value in inputs]
    for layer in net.layers:
        layer._cache_input = vector
        z_values = [
            sum(x * y for x, y in zip(row, vector)) + bias
            for row, bias in zip(layer.weights, layer.biases)
        ]
        layer._cache_z = z_values
        layer._cache_output = _activate_vector(z_values, layer.activation)
        vector = layer._cache_output
    return vector[:]


def _reference_fit(net, X, Y, *, epochs, learning_rate, batch_size, shuffle=True):
    """Reproduce el bucle de entrenamiento original para comparar resultados."""
    X_rows = [[float(value) for value in row] for row in X]
    Y_rows = [[float(value) for value in row] for row in Y]
    sample_count = len(X_rows)
    history = {"loss": []}
    indices = list(range(sample_count))
    for _epoch in range(epochs):
        if shuffle and sample_count > 1:
            net._rng.shuffle(indices)
        epoch_loss = 0.0
        for batch_start in range(0, sample_count, batch_size):
            batch_indices = indices[batch_start : batch_start + batch_size]
            batch_weights = [
                [[0.0] * layer.input_size for _ in range(layer.output_size)]
                for layer in net.layers
            ]
            batch_biases = [[0.0] * layer.output_size for layer in net.layers]
            for sample_index in batch_indices:
                prediction = _reference_forward(net, X_rows[sample_index])
                target = Y_rows[sample_index]
                epoch_loss += _loss_value(prediction, target, net.loss)
                grad = net._output_gradient(prediction, target)
                if net.loss == "mse":
                    output_layer = net.layers[-1]
                    grad = _activation_backward(
                        output_layer.activation,
                        output_layer._cache_z,
                        output_layer._cache_output,
                        grad,
                    )
                for layer_index in range(len(net.layers) - 1, -1, -1):
                    layer = net.layers[layer_index]
                    grad_input, grad_weights, grad_biases = _reference_gradients(layer, grad)
                    for row_index, row in enumerate(grad_weights):
                        target_row = batch_weights[layer_index][row_index]
                        for col_index, value in enumerate(row):
                            target_row[col_index] += value
                    for index, value in enumerate(grad_biases):
                        batch_biases[layer_index][index] += value
                    if layer_index > 0:
                        previous = net.layers[layer_index - 1]
                        grad = _hadamard(
                            grad_input,
                            _activation_derivative(
                                previous.activation,
                                previous._cache_z,
                                previous._cache_output,
                            ),
                        )
            scale = learning_rate / len(batch_indices)
            for layer_index, layer in enumerate(net.layers):
                layer.apply_gradients(batch_weights[layer_index], batch_biases[layer_index], scale)
        history["loss"].append(epoch_loss / sample_count)
    return history


def _reference_calibration(net, X, Y):
    """Calibracion original: una prediccion completa por cada salida."""
    rows = [[float(value) for value in row] for row in X]
    calibration = []
    for output_index in range(net.output_size):
        actual = [float(row[output_index]) for row in Y]
        predicted = [_reference_forward(net, row)[output_index] for row in rows]
        calibration.append(evaluate_errors(actual, predicted))
    return calibration


def _outcome(function):
    """Resultado comparable por repr (nan == nan) o la excepcion lanzada."""
    try:
        return ("ok", repr(function()))
    except Exception as exc:
        return ("error", type(exc).__name__, str(exc))


def _one_hot_samples(rng, count, blocks, block_size, outputs):
    X, Y = [], []
    for _ in range(count):
        row = [0.0] * (blocks * block_size)
        for block in range(blocks):
            row[block * block_size + rng.randrange(block_size)] = 1.0
        target = [0.0] * outputs
        target[rng.randrange(outputs)] = 1.0
        X.append(row)
        Y.append(target)
    return X, Y


class TestIA(unittest.TestCase):
    def test_dense_layer_forward_math(self):
        layer = DenseLayer(2, 1, activation="linear", seed=1)
        layer.weights = [[1.5, -2.0]]
        layer.biases = [0.5]

        output = layer.forward([2, 3])

        self.assertAlmostEqual(output[0], -2.5)

    def test_data_summary_and_error_metrics(self):
        summary = describe_data([1.0, 2.0, 3.0, 4.0])
        self.assertEqual(summary.count, 4)
        self.assertAlmostEqual(summary.mean, 2.5)
        self.assertGreater(summary.std_dev, 0.0)
        self.assertGreater(summary.expanded_uncertainty, summary.standard_uncertainty)

        errors = evaluate_errors([10.0, 11.0, 12.0], [9.9, 11.2, 11.7], permissible_error=0.5)
        self.assertEqual(errors.count, 3)
        self.assertAlmostEqual(errors.maximum_absolute_error, 0.3)
        self.assertTrue(errors.within_permissible_error)

    def test_dataset_split_and_describe(self):
        dataset = DataSet(
            [[1, 2], [2, 3], [3, 4], [4, 5]],
            [[0], [1], [1], [1]],
        )

        train, test = dataset.split(train_ratio=0.75, shuffle=False)

        self.assertEqual(train.sample_count, 3)
        self.assertEqual(test.sample_count, 1)
        self.assertEqual(len(dataset.describe_features()), 2)
        self.assertEqual(len(dataset.describe_targets()), 1)

    def test_xor_training_from_scratch(self):
        X = [[0, 0], [0, 1], [1, 0], [1, 1]]
        Y = [[0], [1], [1], [0]]

        net = NeuralNetwork(seed=7, learning_rate=0.3, loss="binary_cross_entropy", task="classification")
        net.add_dense(6, input_size=2, activation="tanh")
        net.add_dense(1, activation="sigmoid")

        history = net.fit(X, Y, epochs=5000, batch_size=4, shuffle=False)
        predictions = [net.predict(sample)[0] for sample in X]

        self.assertEqual(len(history["loss"]), 5000)
        self.assertLess(history["loss"][-1], history["loss"][0])
        self.assertLess(predictions[0], 0.2)
        self.assertGreater(predictions[1], 0.8)
        self.assertGreater(predictions[2], 0.8)
        self.assertLess(predictions[3], 0.2)

    def test_classification_labels_and_background_task(self):
        X = [[0, 0], [0, 1], [1, 0], [1, 1]]
        labels = ["no", "yes", "yes", "yes"]

        clf = NeuralNetwork(seed=3, learning_rate=0.3, loss="binary_cross_entropy", task="classification")
        clf.add_dense(6, input_size=2, activation="tanh")
        clf.add_dense(1, activation="sigmoid")

        history = clf.fit_classification(X, labels, epochs=3000, batch_size=4, shuffle=False)
        predictions = [clf.predict_class(sample) for sample in X]
        metrics = clf.classification_metrics(X, labels)

        self.assertEqual(len(history["loss"]), 3000)
        self.assertEqual(predictions, labels)
        self.assertGreater(metrics["accuracy"], 0.95)

        dataset = DataSet(X, [[0], [1], [1], [1]])
        tasks = TaskManager(max_concurrent=1)
        async_clf = NeuralNetwork(seed=5, learning_rate=0.3, loss="binary_cross_entropy", task="classification")
        async_clf.add_dense(6, input_size=2, activation="tanh")
        async_clf.add_dense(1, activation="sigmoid")

        task = submit_training_task(
            tasks,
            async_clf,
            dataset.X,
            labels,
            classification=True,
            epochs=1000,
            batch_size=4,
            shuffle=False,
            name="ia-train-test",
        )

        async_history = task.get(timeout=5)

        self.assertEqual(task.status, "completed")
        self.assertEqual(len(async_history["loss"]), 1000)
        self.assertEqual(async_clf.predict_class([1, 0]), "yes")

    def test_mse_applies_output_activation_derivative(self):
        net = NeuralNetwork(seed=1, learning_rate=0.1, loss="mse")
        output = net.add_dense(1, input_size=1, activation="sigmoid")
        output.weights = [[0.0]]
        output.biases = [0.0]

        net.fit([[1.0]], [[0.0]], epochs=1, shuffle=False)

        self.assertAlmostEqual(output.weights[0][0], -0.025)
        self.assertAlmostEqual(output.biases[0], -0.025)

    def test_mse_softmax_applies_full_output_jacobian(self):
        net = NeuralNetwork(seed=1, learning_rate=0.1, loss="mse")
        output = net.add_dense(2, input_size=1, activation="softmax")
        output.weights = [[0.0], [0.0]]
        output.biases = [0.0, 0.0]

        net.fit([[1.0]], [[1.0, 0.0]], epochs=1, shuffle=False)

        self.assertAlmostEqual(output.weights[0][0], 0.025)
        self.assertAlmostEqual(output.weights[1][0], -0.025)

    def test_categorical_cross_entropy_keeps_softmax_shortcut(self):
        net = NeuralNetwork(
            seed=1,
            learning_rate=0.1,
            loss="categorical_cross_entropy",
            task="classification",
        )
        output = net.add_dense(2, input_size=1, activation="softmax")
        output.weights = [[0.0], [0.0]]
        output.biases = [0.0, 0.0]

        net.fit([[1.0]], [[1.0, 0.0]], epochs=1, shuffle=False)

        self.assertAlmostEqual(output.weights[0][0], 0.05)
        self.assertAlmostEqual(output.weights[1][0], -0.05)

    def test_predict_metrics_honors_confidence_and_permissible_error(self):
        net = NeuralNetwork(seed=2, learning_rate=0.01, loss="mse")
        net.add_dense(1, input_size=1, activation="linear")
        net.fit(
            [[0.0], [1.0], [2.0]],
            [[0.0], [2.0], [4.0]],
            epochs=1,
            shuffle=False,
        )

        prediction = net.predict([1.0])[0]
        report = net.predict_with_metrics(
            [1.0],
            expected=prediction + 1.0,
            permissible_error=2.0,
            confidence=0.5,
        )
        metrics = report["metrics"]

        self.assertEqual(metrics["maximum_permissible_error"], 2.0)
        self.assertTrue(metrics["within_permissible_error"])
        self.assertEqual(metrics["confidence"], 0.5)
        self.assertAlmostEqual(
            metrics["coverage_factor"],
            NormalDist().inv_cdf(0.75),
        )
        self.assertAlmostEqual(
            metrics["uncertainty"],
            metrics["coverage_factor"] * metrics["standard_uncertainty"],
        )

    def test_fit_classification_rejects_unknown_labels(self):
        net = NeuralNetwork(
            seed=3,
            loss="binary_cross_entropy",
            task="classification",
        )
        net.add_dense(1, input_size=1, activation="sigmoid")

        with self.assertRaisesRegex(ValueError, "Unknown classification label"):
            net.fit_classification(
                [[0], [1]],
                ["no", "unknown"],
                classes=["no", "yes"],
                epochs=1,
            )

    def test_permissible_error_must_be_non_negative(self):
        with self.assertRaises(ValueError):
            evaluate_errors([1.0], [1.0], permissible_error=-0.1)

    def _twin_networks(self, seed, loss, input_size, layers):
        nets = []
        for _ in range(2):
            task = "classification" if loss == "categorical_cross_entropy" else "regression"
            net = NeuralNetwork(seed=seed, learning_rate=0.1, loss=loss, task=task)
            net.add_dense(layers[0][0], input_size=input_size, activation=layers[0][1])
            for units, activation in layers[1:]:
                net.add_dense(units, activation=activation)
            nets.append(net)
        return nets

    def _assert_matches_reference(self, net, reference, X, Y, **options):
        def run_new():
            history = net.fit(X, Y, epochs=3, learning_rate=0.2, **options)
            return history["loss"], net.calibration()

        def run_reference():
            history = _reference_fit(reference, X, Y, epochs=3, learning_rate=0.2, **options)
            return history["loss"], _reference_calibration(reference, X, Y)

        self.assertEqual(_outcome(run_new), _outcome(run_reference))
        for layer, ref_layer in zip(net.layers, reference.layers):
            self.assertEqual(repr(layer.weights), repr(ref_layer.weights))
            self.assertEqual(repr(layer.biases), repr(ref_layer.biases))
            self.assertIsNone(layer._weights_finite)

    def test_fit_matches_reference_on_sparse_and_dense_inputs(self):
        rng = random.Random(7)
        sparse_X, sparse_Y = _one_hot_samples(rng, 12, blocks=4, block_size=6, outputs=5)
        sparse_X[3] = [0.0] * 24
        sparse_X[5] = [rng.uniform(-1.0, 1.0) for _ in range(24)]
        # Justo en el umbral (12 de 24 no nulos usa la ruta densa) y justo debajo.
        sparse_X[7] = [rng.uniform(0.5, 1.0) if index % 2 else 0.0 for index in range(24)]
        sparse_X[8] = [rng.uniform(0.5, 1.0) if index % 2 and index > 1 else 0.0 for index in range(24)]
        dense_X = [[rng.uniform(-1.0, 1.0) for _ in range(24)] for _ in range(12)]
        regression_Y = [[rng.uniform(-1.0, 1.0) for _ in range(5)] for _ in range(12)]
        cases = [
            ("categorical_cross_entropy", [(8, "tanh"), (5, "softmax")], sparse_Y),
            ("categorical_cross_entropy", [(8, "relu"), (6, "tanh"), (5, "softmax")], sparse_Y),
            ("mse", [(8, "tanh"), (5, "linear")], regression_Y),
            ("mse", [(8, "relu"), (6, "sigmoid"), (5, "softmax")], sparse_Y),
        ]
        for loss, layers, Y in cases:
            for X in (sparse_X, dense_X):
                for batch_size, shuffle in ((1, True), (5, True), (5, False)):
                    with self.subTest(
                        loss=loss, layers=layers, dense=X is dense_X, batch=batch_size, shuffle=shuffle
                    ):
                        net, reference = self._twin_networks(3, loss, 24, layers)
                        self._assert_matches_reference(
                            net, reference, X, Y, batch_size=batch_size, shuffle=shuffle
                        )

    def test_fit_matches_reference_with_non_finite_values(self):
        inf, nan = float("inf"), float("nan")
        rng = random.Random(17)
        X, Y = _one_hot_samples(rng, 8, blocks=4, block_size=6, outputs=5)
        regression_Y = [[rng.uniform(-1.0, 1.0) for _ in range(5)] for _ in range(8)]
        inf_X = [row[:] for row in X]
        inf_X[2][4] = inf
        nan_X = [row[:] for row in X]
        nan_X[6][10] = nan

        def set_weight(layer_index, value):
            def mutate(nets):
                for net in nets:
                    net.layers[layer_index].weights[1][7] = value
            return mutate

        cases = [
            ("inf input", inf_X, None),
            ("nan input", nan_X, None),
            ("inf weight layer 0", X, set_weight(0, inf)),
            ("nan weight layer 0", X, set_weight(0, nan)),
            ("inf weight layer 1", X, set_weight(1, inf)),
        ]
        configs = [
            ("categorical_cross_entropy", [(8, "tanh"), (6, "relu"), (5, "softmax")], Y),
            ("mse", [(8, "relu"), (5, "linear")], regression_Y),
        ]
        for name, inputs, mutate in cases:
            for loss, layers, targets in configs:
                for batch_size in (1, 4):
                    with self.subTest(case=name, loss=loss, batch=batch_size):
                        net, reference = self._twin_networks(5, loss, 24, layers)
                        if mutate is not None:
                            mutate((net, reference))
                        self._assert_matches_reference(
                            net, reference, inputs, targets, batch_size=batch_size
                        )

    def test_forward_and_gradients_match_reference_with_non_finite_weights(self):
        for value in (float("inf"), float("nan")):
            layer = DenseLayer(10, 4, activation="linear", seed=5)
            layer.weights[2][3] = value
            reference = NeuralNetwork(seed=1)
            reference.layers.append(layer)
            vector = [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 2.5, 0.0, 0.0]
            with self.subTest(weight=value):
                outputs = layer.forward(vector)
                self.assertEqual(repr(outputs), repr(_reference_forward(reference, vector)))
                for grad in ([0.3, -0.7, 0.0, 1.2], [0.0, 0.0, 0.0, 0.0]):
                    self.assertEqual(
                        repr(layer.gradients(grad)), repr(_reference_gradients(layer, grad))
                    )

    def test_gradients_api_matches_reference(self):
        rng = random.Random(11)
        layer = DenseLayer(10, 4, activation="tanh", seed=5)
        grads = [[0.3, -0.7, 0.0, 1.2], [0.0, 0.0, 0.0, 0.0], [0.5, 0.25, -0.125, 2.0]]
        inputs = [
            [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 2.5, 0.0, 0.0],
            [0.0] * 10,
            [rng.uniform(-1.0, 1.0) for _ in range(10)],
        ]
        for vector in inputs:
            layer.forward(vector)
            for grad in grads:
                with self.subTest(inputs=vector, grad=grad):
                    self.assertEqual(layer.gradients(grad), _reference_gradients(layer, grad))

    def test_forward_handles_all_zero_and_fully_dense_inputs(self):
        rng = random.Random(13)
        layer = DenseLayer(8, 3, activation="linear", seed=2)
        layer.biases = [0.5, -0.25, 1.0]
        for vector in ([0.0] * 8, [rng.uniform(0.1, 1.0) for _ in range(8)]):
            with self.subTest(inputs=vector):
                expected = [
                    sum(w * x for w, x in zip(row, vector)) + bias
                    for row, bias in zip(layer.weights, layer.biases)
                ]
                self.assertEqual(layer.forward(vector), expected)
                self.assertEqual(layer._cache_input, vector)
        self.assertEqual(layer.forward([0.0] * 8), [0.5, -0.25, 1.0])


if __name__ == "__main__":
    unittest.main()
