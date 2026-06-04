import unittest
from datetime import UTC, datetime

from wsbuilder.orm import (
    BooleanField,
    Database,
    DateTimeField,
    IntegerField,
    JSONField,
    Model,
    TextField,
)


class User(Model):
    __tablename__ = "users"

    id = IntegerField(primary_key=True, auto_increment=True)
    username = TextField(unique=True, index=True, null=False)
    age = IntegerField(default=0, null=False)
    active = BooleanField(default=True, null=False)


class Profile(Model):
    __tablename__ = "profiles"

    id = IntegerField(primary_key=True, auto_increment=True)
    username = TextField(unique=True, null=False)
    meta = JSONField(default=dict, null=False)
    created_at = DateTimeField(default=lambda: datetime.now(UTC), null=False)


class TestORM(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        User.create_table(self.db)
        Profile.create_table(self.db)

    def tearDown(self):
        self.db.close()

    def test_crud_and_filters(self):
        a = User.create(self.db, username="alice", age=30, active=True)
        b = User.create(self.db, username="bob", age=18, active=True)
        c = User.create(self.db, username="carol", age=16, active=False)

        self.assertEqual(a.id, 1)
        self.assertEqual(b.id, 2)
        self.assertEqual(c.id, 3)

        adults = User.objects(self.db).filter(age__gte=18).order_by("-age").all()
        self.assertEqual([u.username for u in adults], ["alice", "bob"])

        starts_with_b = User.objects(self.db).filter(username__startswith="b").first()
        self.assertIsNotNone(starts_with_b)
        self.assertEqual(starts_with_b.username, "bob")

        rows = User.objects(self.db).filter(username__in=["alice", "carol"]).count()
        self.assertEqual(rows, 2)

        updated = User.objects(self.db).filter(username="bob").update(age=19)
        self.assertEqual(updated, 1)
        self.assertEqual(User.get(self.db, username="bob").age, 19)

        deleted = User.objects(self.db).filter(active=False).delete()
        self.assertEqual(deleted, 1)
        self.assertEqual(User.objects(self.db).count(), 2)

    def test_nested_transactions(self):
        with self.db.transaction():
            User.create(self.db, username="outer_1", age=10, active=True)
            with self.assertRaises(RuntimeError):
                with self.db.transaction():
                    User.create(self.db, username="inner", age=99, active=True)
                    raise RuntimeError("force rollback in nested tx")
            User.create(self.db, username="outer_2", age=20, active=True)

        names = [x.username for x in User.objects(self.db).order_by("id").all()]
        self.assertEqual(names, ["outer_1", "outer_2"])

    def test_json_and_datetime_fields(self):
        p = Profile.create(self.db, username="alice", meta={"role": "admin"})
        self.assertIsInstance(p.created_at, datetime)

        loaded = Profile.get(self.db, username="alice")
        self.assertEqual(loaded.meta["role"], "admin")
        self.assertIsInstance(loaded.created_at, datetime)

    def test_exclude_and_values(self):
        User.create(self.db, username="a", age=10, active=True)
        User.create(self.db, username="b", age=20, active=True)
        User.create(self.db, username="c", age=30, active=False)

        usernames = [
            x["username"]
            for x in User.objects(self.db).exclude(active=False).order_by("username").values("username")
        ]
        self.assertEqual(usernames, ["a", "b"])


if __name__ == "__main__":
    unittest.main()
