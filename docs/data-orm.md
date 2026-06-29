# Datos y ORM

## `Database`

`Database` es la capa SQLite incluida. Por defecto funciona bien como embebida
para demos, herramientas locales y servicios pequenos o medianos.

Constructor tipico:

```python
from wsbuilder import Database

db = Database(
    "data/app.sqlite3",
    enable_replicas=True,
    replica_count=2,
    enable_wal=True,
    cache_size_mb=10,
)
```

Capacidades principales:

- `execute`, `executemany`, `fetchone`, `fetchall`, `scalar`.
- `transaction()` para bloques transaccionales.
- `read_replica_*` para lecturas balanceadas.
- `set_pragma`, `get_pragma`, `checkpoint`, `vacuum`, `optimize`.

## Modelos

```python
from datetime import UTC, datetime
from wsbuilder import DateTimeField, IntegerField, JSONField, Model, TextField

class Article(Model):
    __tablename__ = "articles"

    id = IntegerField(primary_key=True, auto_increment=True)
    title = TextField(null=False, index=True)
    body = TextField(default="", null=False)
    tags = JSONField(default=list, null=False)
    created_at = DateTimeField(default=lambda: datetime.now(UTC), null=False)
```

Operaciones tipicas:

```python
Article.create_table(db)
Article.create(db, title="Hola", body="Contenido", tags=["docs"])
first = Article.get(db, id=1)
rows = Article.objects(db).filter(title__startswith="H").order_by("-id").all()
```

## `QuerySet`

Operadores usados en la suite actual:

- `field=value`
- `field__gte=value`
- `field__startswith=value`
- `field__in=[...]`

Metodos utiles:

- `filter`, `exclude`
- `order_by`, `order_by_raw`
- `limit`, `offset`, `paginate`
- `all`, `first`, `get`, `values`
- `count`, `exists`, `update`, `delete`, `create`

## Transacciones

```python
with db.transaction():
    Article.create(db, title="A")
    Article.create(db, title="B")
```

Las transacciones se integran con `save()` y `delete()` de los modelos.

## Campos incluidos

- `IntegerField`
- `RealField`
- `TextField`
- `BlobField`
- `BooleanField`
- `DateTimeField`
- `JSONField`

Tambien existen helpers SQL:

- `SQL`
- `create_tables`
- `drop_tables`
- `quote_identifier`
- `validate_identifier`

## Lecturas optimizadas y replicas

`db_replicas.py` amplia el escenario SQLite:

- `SQLite3OptimizationConfig`: pragmas y ajustes de rendimiento.
- `DatabaseReplica`: conexion de solo lectura.
- `DatabaseReplicaPool`: pool round-robin para lecturas.
- `OptimizedDatabase`: base optimizada con replicas activas por defecto.

Usa `OptimizedDatabase` cuando quieres dejar activados WAL, cache y replicas con
un constructor mas orientado a throughput de lectura.
