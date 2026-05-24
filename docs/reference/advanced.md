# Avanzado

Esta pagina cubre piezas utilitarias que no forman parte del flujo basico HTTP, pero si de la API publica.

## Replicas SQLite

- `SQLite3OptimizationConfig`
- `DatabaseReplica`
- `DatabaseReplicaPool`
- `OptimizedDatabase`

Usalas cuando quieras separar lecturas y escribir sobre una base principal.

## Predictor

- `Predictor`

Es una utilidad matematica interna exportada por el paquete. Si vas a exponerla en tu propia API, documenta el contrato de entrada y salida en tu proyecto porque no es un flujo HTTP de primera linea.

