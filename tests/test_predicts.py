import math
import unittest

from wsbuilder import Predictor
from wsbuilder.predicts import matinv


class TestPredictor(unittest.TestCase):
    def test_documented_rank_deficient_example(self):
        predictor = Predictor()

        predictor.fit(
            [[1, 2], [2, 3], [3, 4]],
            [[2], [3], [4]],
        )

        prediction, deviation, lower, upper = predictor.predict([4, 5])
        self.assertAlmostEqual(prediction[0], 5.0)
        self.assertAlmostEqual(deviation[0], 0.0)
        self.assertAlmostEqual(lower[0], 5.0)
        self.assertAlmostEqual(upper[0], 5.0)
        self.assertEqual(predictor.rank, 2)

    def test_underdetermined_multi_output_fit(self):
        predictor = Predictor()

        predictor.fit(
            [[0, 1, 1], [1, 2, 2]],
            [[1, 2], [2, 4]],
        )

        prediction, _, _, _ = predictor.predict([2, 3, 3])
        self.assertAlmostEqual(prediction[0], 3.0)
        self.assertAlmostEqual(prediction[1], 6.0)

    def test_fit_validates_shapes_and_numeric_values(self):
        invalid_cases = [
            ([[1], [2]], [[1]], ValueError),
            ([[1], [2, 3]], [[1], [2]], ValueError),
            ([[1], [2]], [[1], []], ValueError),
            ([[1], ["not-a-number"]], [[1], [2]], TypeError),
            ([[1], [math.inf]], [[1], [2]], ValueError),
        ]

        for X, Y, error_type in invalid_cases:
            with self.subTest(X=X, Y=Y):
                with self.assertRaises(error_type):
                    Predictor().fit(X, Y)

    def test_predict_requires_fit_and_valid_input(self):
        predictor = Predictor()
        with self.assertRaises(RuntimeError):
            predictor.predict([1])

        predictor.fit([[1], [2]], [[2], [4]])
        with self.assertRaises(ValueError):
            predictor.predict([1, 2])
        with self.assertRaises(TypeError):
            predictor.predict(["bad"])
        with self.assertRaises(ValueError):
            predictor.predict([math.nan])

    def test_matrix_inverse_uses_row_pivoting(self):
        inverse = matinv([[0, 1], [1, 0]])
        self.assertEqual(inverse, [[0.0, 1.0], [1.0, 0.0]])

        with self.assertRaises(ValueError):
            matinv([[1, 2], [2, 4]])


if __name__ == "__main__":
    unittest.main()
