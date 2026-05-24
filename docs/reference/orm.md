# ORM

El ORM de `wsbuilder` esta pensado para SQLite y modelos simples.

## Tipos principales

- `Database`
- `Model`
- `QuerySet`
- `Transaction`
- `Field`
- `IntegerField`
- `TextField`
- `RealField`
- `BlobField`
- `BooleanField`
- `DateTimeField`
- `JSONField`
- `SQL`

## Ejemplo

```python
from wsbuilder import Database, IntegerField, Model, TextField

class User(Model):
    id = IntegerField(primary_key=True, auto_increment=True)
    username = TextField(unique=True, index=True, null=False)
    email = TextField(null=False)

db = Database("app.db")
User.create_table(db)

u = User(username="alice", email="alice@example.com")
u.save(db)
```

## QuerySet

Soporta:

- `filter()`
- `exclude()`
- `order_by()`
- `limit()`
- `offset()`
- `count()`
- `update()`
- `delete()`

## Utilidades SQL

- `validate_identifier(name)`
- `quote_identifier(name)`
- `create_tables(db, *models)`
- `drop_tables(db, *models)`

## Buenas practicas

- Usa nombres de columnas estables y validables.
- No construyas SQL dinamico sin pasar por `validate_identifier()`.
- Usa `Transaction` para agrupar escrituras cuando necesites atomicidad.

