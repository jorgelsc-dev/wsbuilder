"""
Modulo de prediccion estadistica basica (sin librerias externas).

Permite entrenar un modelo lineal con multiples entradas y salidas, y luego
predecir valores con desviacion y limites aproximados.

Ejemplo de uso:

    model = Predictor()
    model.fit(
        X=[[1, 2], [2, 3], [3, 4]],
        Y=[[2], [3], [4]],
    )
    pred, desv, lim_inf, lim_sup = model.predict([4, 5])
"""

from __future__ import annotations

import math


_RANK_TOLERANCE = 1e-12


def _coerce_vector(values, *, label, expected_length=None):
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{label} must be an iterable of numbers")
    try:
        raw_values = list(values)
    except TypeError as exc:
        raise TypeError(f"{label} must be an iterable of numbers") from exc

    vector = []
    for index, value in enumerate(raw_values):
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise TypeError(f"{label}[{index}] must be numeric") from exc
        if not math.isfinite(number):
            raise ValueError(f"{label}[{index}] must be finite")
        vector.append(number)

    if expected_length is not None and len(vector) != expected_length:
        raise ValueError(
            f"{label} must have length {expected_length}, got {len(vector)}"
        )
    return vector


def _coerce_matrix(rows, *, label):
    if isinstance(rows, (str, bytes)):
        raise TypeError(f"{label} must be an iterable of numeric rows")
    try:
        raw_rows = list(rows)
    except TypeError as exc:
        raise TypeError(f"{label} must be an iterable of numeric rows") from exc
    if not raw_rows:
        raise ValueError(f"{label} must not be empty")

    matrix = [
        _coerce_vector(row, label=f"{label}[{index}]")
        for index, row in enumerate(raw_rows)
    ]
    width = len(matrix[0])
    if width == 0:
        raise ValueError(f"{label} rows must not be empty")
    if any(len(row) != width for row in matrix[1:]):
        raise ValueError(f"{label} rows must all have the same length")
    return matrix


def _least_squares(matrix, values):
    """Solve a least-squares system using rank-revealing pivoted QR."""
    row_count = len(matrix)
    column_count = len(matrix[0])
    columns = [
        [matrix[row_index][column_index] for row_index in range(row_count)]
        for column_index in range(column_count)
    ]
    permutation = list(range(column_count))
    squared_norms = [dot(column, column) for column in columns]
    largest_norm = math.sqrt(max(squared_norms, default=0.0))
    tolerance = (
        _RANK_TOLERANCE
        * max(row_count, column_count)
        * max(1.0, largest_norm)
    )

    q_columns = []
    upper = [[0.0 for _ in range(column_count)] for _ in range(column_count)]
    rank = 0

    for step in range(min(row_count, column_count)):
        pivot = max(
            range(step, column_count),
            key=lambda index: squared_norms[index],
        )
        if pivot != step:
            columns[step], columns[pivot] = columns[pivot], columns[step]
            squared_norms[step], squared_norms[pivot] = (
                squared_norms[pivot],
                squared_norms[step],
            )
            permutation[step], permutation[pivot] = (
                permutation[pivot],
                permutation[step],
            )
            for previous in range(step):
                upper[previous][step], upper[previous][pivot] = (
                    upper[previous][pivot],
                    upper[previous][step],
                )

        diagonal = math.sqrt(max(0.0, squared_norms[step]))
        if diagonal <= tolerance:
            break

        q_column = [value / diagonal for value in columns[step]]
        q_columns.append(q_column)
        upper[step][step] = diagonal
        rank += 1

        for column_index in range(step + 1, column_count):
            projection = dot(q_column, columns[column_index])
            upper[step][column_index] = projection
            columns[column_index] = [
                value - projection * q_value
                for value, q_value in zip(columns[column_index], q_column)
            ]
            squared_norms[column_index] = dot(
                columns[column_index],
                columns[column_index],
            )

    if rank == 0:
        raise ValueError("design matrix has no independent columns")

    projected = [dot(q_column, values) for q_column in q_columns]
    pivoted_solution = [0.0 for _ in range(column_count)]
    for row_index in range(rank - 1, -1, -1):
        remainder = sum(
            upper[row_index][column_index] * pivoted_solution[column_index]
            for column_index in range(row_index + 1, rank)
        )
        pivoted_solution[row_index] = (
            projected[row_index] - remainder
        ) / upper[row_index][row_index]

    solution = [0.0 for _ in range(column_count)]
    for pivoted_index, original_index in enumerate(permutation):
        solution[original_index] = pivoted_solution[pivoted_index]
    return solution, rank


class Predictor:
    def __init__(self):
        self.n_in = 0
        self.n_out = 0
        self.X = []
        self.Y = []
        self.coefs = []
        self.bias = []
        self.std = []
        self.rank = 0

    def fit(self, X, Y):
        """Fit a multiple-output linear least-squares model."""
        X_rows = _coerce_matrix(X, label="X")
        Y_rows = _coerce_matrix(Y, label="Y")
        if len(Y_rows) != len(X_rows):
            raise ValueError("X and Y must have the same number of samples")

        self.n_in = len(X_rows[0])
        self.n_out = len(Y_rows[0])
        self.X = [row[:] for row in X_rows]
        self.Y = [row[:] for row in Y_rows]
        self.coefs = []
        self.bias = []
        self.std = []

        design = [row + [1.0] for row in X_rows]
        for output_index in range(self.n_out):
            output = [row[output_index] for row in Y_rows]
            coefficients, rank = _least_squares(design, output)
            self.coefs.append(coefficients[:-1])
            self.bias.append(coefficients[-1])
            residuals = [
                expected - (dot(coefficients[:-1], row) + coefficients[-1])
                for row, expected in zip(X_rows, output)
            ]
            degrees_of_freedom = max(1, len(X_rows) - rank)
            self.std.append(
                math.sqrt(
                    sum(residual * residual for residual in residuals)
                    / degrees_of_freedom
                )
            )
            self.rank = rank

    def predict(self, x):
        """Return predictions, residual deviations, and two-sigma limits."""
        if not self.coefs:
            raise RuntimeError("fit() must be called before predict()")
        inputs = _coerce_vector(x, label="x", expected_length=self.n_in)
        prediction = []
        deviation = []
        lower = []
        upper = []
        for output_index in range(self.n_out):
            value = (
                dot(self.coefs[output_index], inputs)
                + self.bias[output_index]
            )
            prediction.append(value)
            deviation.append(self.std[output_index])
            lower.append(value - 2.0 * self.std[output_index])
            upper.append(value + 2.0 * self.std[output_index])
        return prediction, deviation, lower, upper


def transpose(matrix):
    rows = _coerce_matrix(matrix, label="matrix")
    return [list(column) for column in zip(*rows)]


def matmul(a, b):
    left = _coerce_matrix(a, label="A")
    right = _coerce_matrix(b, label="B")
    if len(left[0]) != len(right):
        raise ValueError("A columns must match B rows")
    columns = list(zip(*right))
    return [[dot(row, column) for column in columns] for row in left]


def matvec(matrix, vector):
    rows = _coerce_matrix(matrix, label="matrix")
    values = _coerce_vector(
        vector,
        label="vector",
        expected_length=len(rows[0]),
    )
    return [dot(row, values) for row in rows]


def dot(a, b):
    if len(a) != len(b):
        raise ValueError("vectors must have the same length")
    return sum(x * y for x, y in zip(a, b))


def matinv(matrix):
    """Invert a square matrix using Gauss-Jordan elimination with pivoting."""
    rows = _coerce_matrix(matrix, label="matrix")
    size = len(rows)
    if len(rows[0]) != size:
        raise ValueError("matrix must be square")

    augmented = [
        row[:] + [float(row_index == column_index) for column_index in range(size)]
        for row_index, row in enumerate(rows)
    ]
    scale = max(abs(value) for row in rows for value in row)
    tolerance = _RANK_TOLERANCE * max(1.0, scale) * size

    for diagonal_index in range(size):
        pivot_index = max(
            range(diagonal_index, size),
            key=lambda row_index: abs(augmented[row_index][diagonal_index]),
        )
        pivot_value = augmented[pivot_index][diagonal_index]
        if abs(pivot_value) <= tolerance:
            raise ValueError("matrix is singular")
        if pivot_index != diagonal_index:
            augmented[diagonal_index], augmented[pivot_index] = (
                augmented[pivot_index],
                augmented[diagonal_index],
            )

        pivot_value = augmented[diagonal_index][diagonal_index]
        augmented[diagonal_index] = [
            value / pivot_value for value in augmented[diagonal_index]
        ]
        for row_index in range(size):
            if row_index == diagonal_index:
                continue
            factor = augmented[row_index][diagonal_index]
            augmented[row_index] = [
                value - factor * pivot
                for value, pivot in zip(
                    augmented[row_index],
                    augmented[diagonal_index],
                )
            ]

    return [row[size:] for row in augmented]
