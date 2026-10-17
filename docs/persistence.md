# Persistencia

La capa de persistencia combina un ORM pequeno, helpers de SQLite y replicas de lectura opcionales.

## Base

```python
from wsbuilder import Database

db = Database(":memory:")
```

Opciones comunes:

- `enable_wal=True` para journaling mas robusto.
- `enable_replicas=True` para lecturas en replicas.
- `replica_count` para controlar cuantos readers mantener.

## Modelos

Define modelos con clases `Model` y campos declarativos:

```python
from datetime import UTC, datetime
from wsbuilder import BooleanField, DateTimeField, IntegerField, JSONField, Model, TextField

class User(Model):
    __tablename__ = "users"

    id = IntegerField(primary_key=True, auto_increment=True)
    username = TextField(unique=True, index=True, null=False)
    active = BooleanField(default=True, null=False)
    meta = JSONField(default=dict, null=False)
    created_at = DateTimeField(default=lambda: datetime.now(UTC), null=False)
```

## CRUD rapido

```python
User.create_table(db)
alice = User.create(db, username="alice", active=True, meta={"role": "admin"})
row = User.get(db, username="alice")
rows = User.objects(db).filter(active=True).order_by("username").all()
User.objects(db).filter(username="alice").update(active=False)
User.objects(db).filter(active=False).delete()
```

## QuerySet

`QuerySet` expone patrones conocidos:

- `filter(...)`
- `exclude(...)`
- `order_by(...)`
- `limit(...)`
- `offset(...)`
- `paginate(page, per_page)`
- `values("field1", "field2")`
- `count()` y `exists()`

Tambien soporta operadores como:

- `__gte`
- `__lte`
- `__startswith`
- `__in`
- `__contains`

## Transacciones

```python
with db.transaction():
    User.create(db, username="bob", active=True)
    User.create(db, username="carol", active=True)
```

Si una excepcion rompe el bloque, la transaccion se revierte.

## Replicas de lectura

La familia `db_replicas` agrega lectura optimizada:

- `SQLite3OptimizationConfig`
- `DatabaseReplica`
- `DatabaseReplicaPool`
- `OptimizedDatabase`

Uso tipico:

```python
from wsbuilder import Database

db = Database("app.db", enable_wal=True, enable_replicas=True, replica_count=3)
rows = db.read_replica_fetchall("SELECT * FROM users")
```

En `QuerySet`, puedes forzar el camino de replica con:

```python
User.objects(db).using("replica").all()
```

## Reglas utiles

- Usa `create_tables(db, *models)` para crear varias tablas de una vez.
- Usa `drop_tables(db, *models)` cuando quieras limpiar fixtures o tests.
- `quote_identifier()` y `validate_identifier()` ayudan a evitar SQL inseguro en metadatos.

## Buenas practicas

1. Mantente en `:memory:` para tests.
2. Usa `WAL` en archivos persistentes.
3. Delega lecturas intensivas a replicas cuando el archivo de base lo permita.
4. Cierra la base con `db.close()` o usa context managers.
