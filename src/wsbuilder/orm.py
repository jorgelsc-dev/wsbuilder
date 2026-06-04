"""Flexible SQLite3 ORM utilities for wsbuilder."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import date, datetime

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_WRITE_PREFIXES = {
    "INSERT",
    "UPDATE",
    "DELETE",
    "REPLACE",
    "CREATE",
    "DROP",
    "ALTER",
    "VACUUM",
    "REINDEX",
}


def _to_snake_case(name: str) -> str:
    step1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", step1).lower()


def validate_identifier(name: str) -> str:
    if not isinstance(name, str) or not _IDENTIFIER_RE.match(name):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return name


def quote_identifier(name: str) -> str:
    return f'"{validate_identifier(name)}"'


def _first_keyword(sql: str) -> str:
    stripped = sql.strip()
    if not stripped:
        return ""
    return stripped.split(None, 1)[0].upper()


def _is_write_statement(sql: str) -> bool:
    return _first_keyword(sql) in _WRITE_PREFIXES


class SQL:
    """Raw SQL expression helper for DDL defaults."""

    def __init__(self, expression: str):
        self.expression = expression

    def __str__(self) -> str:
        return self.expression


class Field:
    """Base column definition."""

    def __init__(
        self,
        column_type: str,
        *,
        primary_key: bool = False,
        unique: bool = False,
        null: bool = True,
        default=None,
        index: bool = False,
        check: str | None = None,
    ):
        self.column_type = column_type
        self.primary_key = primary_key
        self.unique = unique
        self.null = null
        self.default = default
        self.index = index
        self.check = check
        self.name: str | None = None

    def to_db(self, value):
        return value

    def from_db(self, value):
        return value

    def python_default(self):
        if callable(self.default):
            return self.default()
        return self.default

    def _sql_literal(self, value) -> str:
        if isinstance(value, SQL):
            return str(value)
        value = self.to_db(value)
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, (int, float)):
            return str(value)
        text = str(value).replace("'", "''")
        return f"'{text}'"

    def ddl_fragment(self) -> str:
        if self.name is None:
            raise RuntimeError("Field name not bound to model")

        parts = [quote_identifier(self.name), self.column_type]
        if self.primary_key:
            parts.append("PRIMARY KEY")
        if self.unique and not self.primary_key:
            parts.append("UNIQUE")
        if not self.null and not self.primary_key:
            parts.append("NOT NULL")
        if self.check:
            parts.append(f"CHECK ({self.check})")
        if self.default is not None and not callable(self.default) and not self.primary_key:
            parts.append("DEFAULT")
            parts.append(self._sql_literal(self.default))
        return " ".join(parts)


class IntegerField(Field):
    def __init__(
        self,
        *,
        primary_key: bool = False,
        unique: bool = False,
        null: bool = True,
        default=None,
        index: bool = False,
        auto_increment: bool = False,
        check: str | None = None,
    ):
        super().__init__(
            "INTEGER",
            primary_key=primary_key,
            unique=unique,
            null=null,
            default=default,
            index=index,
            check=check,
        )
        self.auto_increment = auto_increment

    def ddl_fragment(self) -> str:
        base = super().ddl_fragment()
        if self.primary_key and self.auto_increment:
            return base + " AUTOINCREMENT"
        return base


class RealField(Field):
    def __init__(self, **kwargs):
        super().__init__("REAL", **kwargs)


class TextField(Field):
    def __init__(self, **kwargs):
        super().__init__("TEXT", **kwargs)


class BlobField(Field):
    def __init__(self, **kwargs):
        super().__init__("BLOB", **kwargs)


class BooleanField(IntegerField):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def to_db(self, value):
        if value is None:
            return None
        return 1 if bool(value) else 0

    def from_db(self, value):
        if value is None:
            return None
        return bool(value)


class DateTimeField(TextField):
    """Stores datetimes as ISO-8601 text."""

    def to_db(self, value):
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, date):
            return datetime.combine(value, datetime.min.time()).isoformat()
        return str(value)

    def from_db(self, value):
        if value is None or isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return value


class JSONField(TextField):
    """Stores JSON values as TEXT."""

    def to_db(self, value):
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def from_db(self, value):
        if value is None:
            return None
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value


class ModelMeta(type):
    def __new__(mcls, name, bases, attrs):
        if name == "Model":
            return super().__new__(mcls, name, bases, attrs)

        fields = {}
        for base in bases:
            meta = getattr(base, "_meta", None)
            if meta:
                fields.update(meta["fields"])

        for key, value in list(attrs.items()):
            if isinstance(value, Field):
                value.name = key
                fields[key] = value

        tablename = attrs.get("__tablename__") or _to_snake_case(name)
        validate_identifier(tablename)

        pk_names = [fname for fname, field in fields.items() if field.primary_key]
        if len(pk_names) > 1:
            raise RuntimeError(f"Model {name} has multiple primary keys: {pk_names}")

        attrs["_meta"] = {
            "table": tablename,
            "fields": fields,
            "pk_name": pk_names[0] if pk_names else None,
        }
        return super().__new__(mcls, name, bases, attrs)


class Database:
    """Thread-safe sqlite3 wrapper with nested transaction support and read replicas."""

    def __init__(
        self,
        path: str = ":memory:",
        *,
        shared_cache: bool = False,
        timeout: float = 10.0,
        pragmas: dict | None = None,
        detect_types: int = 0,
        debug: bool = False,
        enable_replicas: bool = False,
        replica_count: int = 3,
        enable_wal: bool = True,
        cache_size_mb: int = 10,
    ):
        self.debug = debug
        self._lock = threading.RLock()
        self._tx_depth = threading.local()
        self._replicas = None

        if shared_cache:
            dsn = "file::memory:?cache=shared" if path == ":memory:" else path
            self._conn = sqlite3.connect(
                dsn,
                check_same_thread=False,
                timeout=timeout,
                detect_types=detect_types,
                uri=True,
            )
        else:
            self._conn = sqlite3.connect(
                path,
                check_same_thread=False,
                timeout=timeout,
                detect_types=detect_types,
            )

        self._conn.row_factory = sqlite3.Row
        
        # Default optimized pragmas
        base_pragmas = {
            "foreign_keys": 1,
            "busy_timeout": int(timeout * 1000),
        }
        
        # Add SQLite3 optimizations
        if enable_wal:
            base_pragmas["journal_mode"] = "WAL"
            base_pragmas["wal_autocheckpoint"] = 1000
        
        base_pragmas["cache_size"] = -cache_size_mb
        base_pragmas["synchronous"] = "NORMAL"
        base_pragmas["temp_store"] = "MEMORY"
        base_pragmas["mmap_size"] = 30000000
        base_pragmas["automatic_index"] = 1
        
        if pragmas:
            base_pragmas.update(pragmas)
        for key, value in base_pragmas.items():
            self.set_pragma(key, value)
        
        # Enable read replicas if needed
        if enable_replicas and path != ":memory:":
            from . import db_replicas
            self._replicas = db_replicas.DatabaseReplicaPool(
                path,
                replica_count=replica_count,
                timeout=timeout,
                detect_types=detect_types,
            )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def close(self):
        with self._lock:
            if self._replicas:
                self._replicas.close_all()
            self._conn.close()

    def _log(self, *parts):
        if self.debug:
            print("[ORM]", *parts)

    def set_pragma(self, name: str, value):
        validate_identifier(name)
        sql = f"PRAGMA {name}={value}"
        with self._lock:
            self._log(sql)
            self._conn.execute(sql)

    def get_pragma(self, name: str):
        validate_identifier(name)
        sql = f"PRAGMA {name}"
        with self._lock:
            self._log(sql)
            cur = self._conn.execute(sql)
            row = cur.fetchone()
            return None if row is None else row[0]

    def checkpoint(self, mode: str = "RESTART"):
        """Perform WAL checkpoint to reclaim space."""
        validate_identifier(mode)
        with self._lock:
            self._log(f"PRAGMA wal_checkpoint({mode})")
            self._conn.execute(f"PRAGMA wal_checkpoint({mode})")

    def vacuum(self):
        """Vacuum the database to reclaim disk space."""
        with self._lock:
            self._log("VACUUM")
            self._conn.execute("VACUUM")

    def optimize(self):
        """Run SQLite3 PRAGMA optimize."""
        with self._lock:
            self._log("PRAGMA optimize")
            self._conn.execute("PRAGMA optimize")

    def _get_tx_depth(self) -> int:
        return getattr(self._tx_depth, "value", 0)

    def _set_tx_depth(self, value: int):
        self._tx_depth.value = value

    def in_transaction(self) -> bool:
        return self._get_tx_depth() > 0

    def execute(self, sql: str, params=None, *, commit: bool | None = None):
        if params is None:
            params = ()
        if commit is None:
            commit = _is_write_statement(sql)
        with self._lock:
            self._log("SQL:", sql.strip(), "params=", params)
            cur = self._conn.execute(sql, tuple(params))
            if commit and not self.in_transaction():
                self._conn.commit()
            return cur

    def executemany(
        self,
        sql: str,
        seq_of_params,
        *,
        commit: bool | None = None,
    ) -> int:
        if commit is None:
            commit = _is_write_statement(sql)
        with self._lock:
            self._log("SQL many:", sql.strip())
            cur = self._conn.executemany(sql, [tuple(p) for p in seq_of_params])
            if commit and not self.in_transaction():
                self._conn.commit()
            return cur.rowcount

    def fetchone(self, sql: str, params=None):
        return self.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params=None):
        return self.execute(sql, params).fetchall()

    def scalar(self, sql: str, params=None, default=None):
        row = self.fetchone(sql, params)
        if row is None:
            return default
        return row[0]

    def read_replica_execute(self, sql: str, params=None):
        """Execute a read-only query on a replica if available."""
        if self._replicas is None:
            return self.execute(sql, params)
        return self._replicas.execute(sql, params)

    def read_replica_fetchone(self, sql: str, params=None):
        """Fetch one row from a replica if available."""
        if self._replicas is None:
            return self.fetchone(sql, params)
        return self._replicas.fetchone(sql, params)

    def read_replica_fetchall(self, sql: str, params=None):
        """Fetch all rows from a replica if available."""
        if self._replicas is None:
            return self.fetchall(sql, params)
        return self._replicas.fetchall(sql, params)

    def read_replica_scalar(self, sql: str, params=None, default=None):
        """Fetch a scalar value from a replica if available."""
        if self._replicas is None:
            return self.scalar(sql, params, default)
        return self._replicas.scalar(sql, params, default)

    def transaction(self):
        return Transaction(self)


class Transaction:
    def __init__(self, db: Database):
        self.db = db
        self._savepoint_name = None
        self._entered = False

    def __enter__(self):
        depth = self.db._get_tx_depth()
        if depth == 0:
            with self.db._lock:
                self.db._log("BEGIN")
                self.db._conn.execute("BEGIN")
            self.db._set_tx_depth(1)
        else:
            self._savepoint_name = f"sp_{depth}"
            with self.db._lock:
                self.db._log("SAVEPOINT", self._savepoint_name)
                self.db._conn.execute(f"SAVEPOINT {self._savepoint_name}")
            self.db._set_tx_depth(depth + 1)
        self._entered = True
        return self

    def __exit__(self, exc_type, exc, tb):
        if not self._entered:
            return False
        depth = self.db._get_tx_depth()
        if exc_type is None:
            if self._savepoint_name is None:
                with self.db._lock:
                    self.db._log("COMMIT")
                    self.db._conn.commit()
                self.db._set_tx_depth(0)
            else:
                with self.db._lock:
                    self.db._log("RELEASE", self._savepoint_name)
                    self.db._conn.execute(f"RELEASE SAVEPOINT {self._savepoint_name}")
                self.db._set_tx_depth(depth - 1)
        else:
            if self._savepoint_name is None:
                with self.db._lock:
                    self.db._log("ROLLBACK")
                    self.db._conn.rollback()
                self.db._set_tx_depth(0)
            else:
                with self.db._lock:
                    self.db._log("ROLLBACK TO", self._savepoint_name)
                    self.db._conn.execute(f"ROLLBACK TO SAVEPOINT {self._savepoint_name}")
                    self.db._conn.execute(f"RELEASE SAVEPOINT {self._savepoint_name}")
                self.db._set_tx_depth(depth - 1)
        self._entered = False
        return False


class QuerySet:
    """Composable query builder for a model."""

    def __init__(
        self,
        db: Database,
        model,
        *,
        where_clauses: list[str] | None = None,
        params: list | None = None,
        order_by_clauses: list[str] | None = None,
        limit_value: int | None = None,
        offset_value: int | None = None,
    ):
        self.db = db
        self.model = model
        self.where_clauses = where_clauses or []
        self.params = params or []
        self.order_by_clauses = order_by_clauses or []
        self.limit_value = limit_value
        self.offset_value = offset_value

    def _clone(self, **overrides):
        data = {
            "db": self.db,
            "model": self.model,
            "where_clauses": list(self.where_clauses),
            "params": list(self.params),
            "order_by_clauses": list(self.order_by_clauses),
            "limit_value": self.limit_value,
            "offset_value": self.offset_value,
        }
        data.update(overrides)
        return QuerySet(**data)

    def _column(self, name: str) -> str:
        if name not in self.model._meta["fields"]:
            raise ValueError(f"Unknown field for {self.model.__name__}: {name}")
        return quote_identifier(name)

    def _field_and_op(self, key: str):
        if "__" in key:
            return key.split("__", 1)
        return key, "eq"

    def _build_lookup(self, field_name: str, op: str, value):
        column = self._column(field_name)
        field = self.model._meta["fields"][field_name]

        if op == "eq":
            if value is None:
                return f"{column} IS NULL", []
            return f"{column} = ?", [field.to_db(value)]
        if op == "ne":
            if value is None:
                return f"{column} IS NOT NULL", []
            return f"{column} != ?", [field.to_db(value)]
        if op == "gt":
            return f"{column} > ?", [field.to_db(value)]
        if op == "gte":
            return f"{column} >= ?", [field.to_db(value)]
        if op == "lt":
            return f"{column} < ?", [field.to_db(value)]
        if op == "lte":
            return f"{column} <= ?", [field.to_db(value)]
        if op == "like":
            return f"{column} LIKE ?", [value]
        if op == "ilike":
            return f"LOWER({column}) LIKE LOWER(?)", [value]
        if op in {"contains", "icontains"}:
            pattern = f"%{value}%"
            if op == "icontains":
                return f"LOWER({column}) LIKE LOWER(?)", [pattern]
            return f"{column} LIKE ?", [pattern]
        if op in {"startswith", "istartswith"}:
            pattern = f"{value}%"
            if op == "istartswith":
                return f"LOWER({column}) LIKE LOWER(?)", [pattern]
            return f"{column} LIKE ?", [pattern]
        if op in {"endswith", "iendswith"}:
            pattern = f"%{value}"
            if op == "iendswith":
                return f"LOWER({column}) LIKE LOWER(?)", [pattern]
            return f"{column} LIKE ?", [pattern]
        if op in {"in", "not_in"}:
            values = list(value or [])
            if not values:
                if op == "in":
                    return "1 = 0", []
                return "1 = 1", []
            placeholders = ",".join("?" for _ in values)
            db_values = [field.to_db(v) for v in values]
            keyword = "IN" if op == "in" else "NOT IN"
            return f"{column} {keyword} ({placeholders})", db_values
        if op == "isnull":
            return (f"{column} IS NULL", []) if value else (f"{column} IS NOT NULL", [])
        raise ValueError(f"Unsupported filter operator: {op}")

    def where_raw(self, clause: str, *params):
        clone = self._clone()
        clone.where_clauses.append(f"({clause})")
        clone.params.extend(params)
        return clone

    def filter(self, **kwargs):
        clone = self._clone()
        for key, value in kwargs.items():
            field_name, op = self._field_and_op(key)
            clause, clause_params = self._build_lookup(field_name, op, value)
            clone.where_clauses.append(clause)
            clone.params.extend(clause_params)
        return clone

    def exclude(self, **kwargs):
        clauses = []
        params = []
        for key, value in kwargs.items():
            field_name, op = self._field_and_op(key)
            clause, clause_params = self._build_lookup(field_name, op, value)
            clauses.append(clause)
            params.extend(clause_params)
        if not clauses:
            return self
        return self.where_raw(f"NOT ({' AND '.join(clauses)})", *params)

    def order_by(self, *fields: str):
        clone = self._clone()
        for item in fields:
            direction = "ASC"
            name = item
            if item.startswith("-"):
                direction = "DESC"
                name = item[1:]
            elif item.startswith("+"):
                name = item[1:]
            clone.order_by_clauses.append(f"{self._column(name)} {direction}")
        return clone

    def order_by_raw(self, clause: str):
        clone = self._clone()
        clone.order_by_clauses.append(clause)
        return clone

    def limit(self, n: int):
        return self._clone(limit_value=n)

    def offset(self, n: int):
        return self._clone(offset_value=n)

    def paginate(self, page: int, per_page: int):
        page = max(page, 1)
        per_page = max(per_page, 1)
        return self.limit(per_page).offset((page - 1) * per_page)

    def _build_select(self, columns: list[str] | None = None):
        table = quote_identifier(self.model._meta["table"])
        if columns:
            column_sql = ", ".join(columns)
        else:
            model_columns = [quote_identifier(name) for name in self.model._meta["fields"]]
            column_sql = ", ".join(model_columns)
        sql = f"SELECT {column_sql} FROM {table}"
        params = list(self.params)
        if self.where_clauses:
            sql += " WHERE " + " AND ".join(self.where_clauses)
        if self.order_by_clauses:
            sql += " ORDER BY " + ", ".join(self.order_by_clauses)
        if self.limit_value is not None:
            sql += " LIMIT ?"
            params.append(self.limit_value)
        if self.offset_value is not None:
            sql += " OFFSET ?"
            params.append(self.offset_value)
        return sql, params

    def all(self):
        sql, params = self._build_select()
        rows = self.db.execute(sql, params).fetchall()
        return [self.model.from_row(row) for row in rows]

    def first(self):
        sql, params = self.limit(1)._build_select()
        row = self.db.execute(sql, params).fetchone()
        if row is None:
            return None
        return self.model.from_row(row)

    def get(self, **kwargs):
        rows = self.filter(**kwargs).limit(2).all()
        if not rows:
            raise LookupError("No rows found")
        if len(rows) > 1:
            raise LookupError("Multiple rows found for get()")
        return rows[0]

    def values(self, *fields: str):
        if fields:
            columns = [quote_identifier(name) for name in fields]
            selected = list(fields)
        else:
            selected = list(self.model._meta["fields"].keys())
            columns = [quote_identifier(name) for name in selected]
        sql, params = self._build_select(columns=columns)
        rows = self.db.execute(sql, params).fetchall()
        out = []
        for row in rows:
            item = {}
            for key in selected:
                item[key] = row[key]
            out.append(item)
        return out

    def count(self) -> int:
        table = quote_identifier(self.model._meta["table"])
        sql = f"SELECT COUNT(*) FROM {table}"
        params = list(self.params)
        if self.where_clauses:
            sql += " WHERE " + " AND ".join(self.where_clauses)
        return int(self.db.scalar(sql, params, default=0))

    def exists(self) -> bool:
        return self.limit(1).count() > 0

    def update(self, **kwargs) -> int:
        if not kwargs:
            return 0
        table = quote_identifier(self.model._meta["table"])
        params = []
        set_parts = []
        for key, value in kwargs.items():
            if key not in self.model._meta["fields"]:
                raise ValueError(f"Unknown field for update: {key}")
            field = self.model._meta["fields"][key]
            set_parts.append(f"{quote_identifier(key)} = ?")
            params.append(field.to_db(value))
        sql = f"UPDATE {table} SET " + ", ".join(set_parts)
        if self.where_clauses:
            sql += " WHERE " + " AND ".join(self.where_clauses)
            params.extend(self.params)
        cur = self.db.execute(sql, params)
        return cur.rowcount

    def delete(self) -> int:
        table = quote_identifier(self.model._meta["table"])
        sql = f"DELETE FROM {table}"
        params = list(self.params)
        if self.where_clauses:
            sql += " WHERE " + " AND ".join(self.where_clauses)
        cur = self.db.execute(sql, params)
        return cur.rowcount

    def create(self, **kwargs):
        obj = self.model(**kwargs)
        obj.save(self.db)
        return obj


class Model(metaclass=ModelMeta):
    __tablename__ = None

    def __init__(self, **kwargs):
        fields = self._meta["fields"]
        for name, field in fields.items():
            value = kwargs[name] if name in kwargs else field.python_default()
            setattr(self, name, value)

    @classmethod
    def create_table(cls, db: Database, if_not_exists: bool = True):
        fields = cls._meta["fields"]
        if not fields:
            raise RuntimeError(f"Model {cls.__name__} has no fields")
        table = quote_identifier(cls._meta["table"])
        ine = "IF NOT EXISTS " if if_not_exists else ""
        column_sql = ", ".join(field.ddl_fragment() for field in fields.values())
        db.execute(f"CREATE TABLE {ine}{table} ({column_sql})")

        for name, field in fields.items():
            if field.index and not field.primary_key:
                idx_name = f"idx_{cls._meta['table']}_{name}"
                unique = "UNIQUE " if field.unique else ""
                db.execute(
                    f"CREATE {unique}INDEX IF NOT EXISTS {quote_identifier(idx_name)} "
                    f"ON {table} ({quote_identifier(name)})"
                )

    @classmethod
    def drop_table(cls, db: Database, if_exists: bool = True):
        ie = "IF EXISTS " if if_exists else ""
        table = quote_identifier(cls._meta["table"])
        db.execute(f"DROP TABLE {ie}{table}")

    @classmethod
    def objects(cls, db: Database) -> QuerySet:
        return QuerySet(db, cls)

    @classmethod
    def raw(cls, db: Database, sql: str, params=None):
        rows = db.execute(sql, params).fetchall()
        return [cls.from_row(row) for row in rows]

    @classmethod
    def from_row(cls, row):
        data = {}
        for name, field in cls._meta["fields"].items():
            data[name] = field.from_db(row[name]) if name in row.keys() else None
        return cls(**data)

    @classmethod
    def create(cls, db: Database, **kwargs):
        obj = cls(**kwargs)
        obj.save(db)
        return obj

    @classmethod
    def get(cls, db: Database, **kwargs):
        return cls.objects(db).get(**kwargs)

    @classmethod
    def filter(cls, db: Database, **kwargs):
        return cls.objects(db).filter(**kwargs)

    def to_dict(self):
        return {name: getattr(self, name) for name in self._meta["fields"].keys()}

    def pk_value(self):
        pk_name = self._meta["pk_name"]
        if not pk_name:
            return None
        return getattr(self, pk_name, None)

    def save(self, db: Database):
        fields = self._meta["fields"]
        table = quote_identifier(self._meta["table"])
        pk_name = self._meta["pk_name"]
        pk_value = self.pk_value()

        if pk_name and pk_value is not None:
            set_parts = []
            params = []
            for name, field in fields.items():
                if name == pk_name:
                    continue
                set_parts.append(f"{quote_identifier(name)} = ?")
                params.append(field.to_db(getattr(self, name)))
            params.append(fields[pk_name].to_db(pk_value))
            sql = (
                f"UPDATE {table} SET " + ", ".join(set_parts) +
                f" WHERE {quote_identifier(pk_name)} = ?"
            )
            cur = db.execute(sql, params)
            return cur.rowcount

        cols = []
        placeholders = []
        params = []
        for name, field in fields.items():
            value = getattr(self, name)
            if field.primary_key and isinstance(field, IntegerField) and value is None:
                continue
            cols.append(quote_identifier(name))
            placeholders.append("?")
            params.append(field.to_db(value))
        sql = (
            f"INSERT INTO {table} (" + ", ".join(cols) + ") VALUES (" +
            ", ".join(placeholders) + ")"
        )
        cur = db.execute(sql, params)
        if pk_name and isinstance(fields[pk_name], IntegerField) and getattr(self, pk_name) is None:
            setattr(self, pk_name, cur.lastrowid)
        return 1

    def delete(self, db: Database):
        pk_name = self._meta["pk_name"]
        if not pk_name:
            raise RuntimeError("delete() requires a primary key")
        pk_value = self.pk_value()
        if pk_value is None:
            return 0
        table = quote_identifier(self._meta["table"])
        sql = f"DELETE FROM {table} WHERE {quote_identifier(pk_name)} = ?"
        cur = db.execute(sql, (pk_value,))
        return cur.rowcount


def create_tables(db: Database, *models):
    for model in models:
        model.create_table(db)


def drop_tables(db: Database, *models):
    for model in models:
        model.drop_table(db)


__all__ = [
    "BlobField",
    "BooleanField",
    "Database",
    "DateTimeField",
    "Field",
    "IntegerField",
    "JSONField",
    "Model",
    "QuerySet",
    "RealField",
    "SQL",
    "TextField",
    "Transaction",
    "create_tables",
    "drop_tables",
    "quote_identifier",
    "validate_identifier",
]
