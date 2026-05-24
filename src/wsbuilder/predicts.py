"""
Módulo de predicción estadística básica (sin librerías externas).
Permite entrenar un modelo simple con múltiples entradas y salidas,
y luego predecir valores futuros con incertidumbre, desviación y límites.

Ejemplo de uso:

    # Entrenamiento
    model = Predictor()
    model.fit(
        X=[[1, 2], [2, 3], [3, 4]],  # Entradas (n muestras x m features)
        Y=[[2], [3], [4]]            # Salidas (n muestras x k salidas)
    )
    # Predicción
    pred, desv, lim_inf, lim_sup = model.predict([4, 5])

"""

class Predictor:
    def __init__(self):
        self.n_in = 0
        self.n_out = 0
        self.X = []
        self.Y = []
        self.coefs = []  # Coeficientes para cada salida
        self.bias = []   # Sesgo para cada salida
        self.std = []    # Desviación estándar para cada salida

    def fit(self, X, Y):
        """
        Aprende una relación lineal simple entre X y Y.
        X: lista de listas (n muestras x m entradas)
        Y: lista de listas (n muestras x k salidas)
        """
        n = len(X)
        if n == 0 or len(Y) != n:
            raise ValueError("Datos de entrada y salida deben tener igual longitud y no vacíos")
        self.n_in = len(X[0])
        self.n_out = len(Y[0])
        self.X = X
        self.Y = Y
        self.coefs = []
        self.bias = []
        self.std = []
        # Para cada salida, ajusta una regresión lineal simple (sin librerías)
        for j in range(self.n_out):
            # Ajuste por mínimos cuadrados para cada salida respecto a cada entrada
            # y = a1*x1 + a2*x2 + ... + b
            # Usamos pseudo-inversa para resolver Ax = y
            A = [x + [1] for x in X]  # Añade columna de 1 para el sesgo
            y = [row[j] for row in Y]
            # Calcula (A^T A)^-1 A^T y
            At = transpose(A)
            AtA = matmul(At, A)
            inv_AtA = matinv(AtA)
            At_y = matvec(At, y)
            coef = matvec(inv_AtA, At_y)
            self.coefs.append(coef[:-1])
            self.bias.append(coef[-1])
            # Calcula desviación estándar de residuos
            residuos = [y_i - (dot(coef[:-1], x) + coef[-1]) for x, y_i in zip(X, y)]
            std = (sum((r ** 2 for r in residuos)) / max(1, n - 2)) ** 0.5
            self.std.append(std)

    def predict(self, x):
        """
        Predice la(s) salida(s) para una entrada x.
        Devuelve:
            pred: lista de predicciones
            std: lista de desviaciones estándar
            lim_inf: lista de límites inferiores (pred - 2*std)
            lim_sup: lista de límites superiores (pred + 2*std)
        """
        if len(x) != self.n_in:
            raise ValueError(f"Se esperaban {self.n_in} entradas, se recibieron {len(x)}")
        pred = []
        desv = []
        lim_inf = []
        lim_sup = []
        for j in range(self.n_out):
            y_pred = dot(self.coefs[j], x) + self.bias[j]
            pred.append(y_pred)
            desv.append(self.std[j])
            lim_inf.append(y_pred - 2 * self.std[j])
            lim_sup.append(y_pred + 2 * self.std[j])
        return pred, desv, lim_inf, lim_sup

# Utilidades matemáticas puras

def transpose(M):
    return [list(row) for row in zip(*M)]

def matmul(A, B):
    # Multiplica matrices A (m x n) y B (n x p)
    result = [[sum(a * b for a, b in zip(row, col)) for col in zip(*B)] for row in A]
    return result

def matvec(A, v):
    # Multiplica matriz A (m x n) por vector v (n)
    return [sum(a * b for a, b in zip(row, v)) for row in A]

def dot(a, b):
    return sum(x * y for x, y in zip(a, b))

def matinv(M):
    # Inversa de matriz cuadrada M (Gauss-Jordan, sin librerías)
    n = len(M)
    AM = [row[:] + [float(i == j) for j in range(n)] for i, row in enumerate(M)]
    for fd in range(n):
        fdScaler = 1.0 / AM[fd][fd]
        for j in range(2 * n):
            AM[fd][j] *= fdScaler
        for i in range(n):
            if i == fd:
                continue
            crScaler = AM[i][fd]
            for j in range(2 * n):
                AM[i][j] -= crScaler * AM[fd][j]
    return [row[n:] for row in AM]
