import unittest

from wsbuilder import DataSet, NeuralNetwork, TaskManager, describe_data, evaluate_errors, submit_training_task
from wsbuilder.ia import DenseLayer


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


if __name__ == "__main__":
    unittest.main()
