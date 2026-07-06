from __future__ import annotations

from eorm import Field, Model


class User(Model):
    class Meta:
        table = "users"

    id: int = Field(primary_key=True, auto_increment=True)
    name: str = Field(max_length=100)
    email: str = Field(unique=True)
    age: int | None = None


def test_model_metadata() -> None:
    assert User.__table__ == "users"
    assert User.__pk__ == "id"
    assert User.__columns__["email"].column_name == "email"
    assert User.__column_info__["id"].primary_key is True
    assert User.__column_info__["id"].auto_increment is True


def test_auto_increment_primary_key_defaults_to_none() -> None:
    user = User(name="Alice", email="alice@example.com")
    assert user.id is None
    assert user.age is None


def test_class_level_field_access_returns_column() -> None:
    """类级别访问字段名应返回 Column 对象，用于构建表达式。"""
    from eorm.expression import Column as ColumnCls

    assert isinstance(User.name, ColumnCls)
    assert User.name.field_name == "name"
    assert isinstance(User.id, ColumnCls)
    assert User.id.field_name == "id"


def test_class_level_field_expression_works() -> None:
    """类级别表达式（如 User.name == 'Alice'）应返回 BinaryExpression。"""
    from eorm.expression import BinaryExpression

    expr = User.name == "Alice"
    assert isinstance(expr, BinaryExpression)

    expr2 = User.age > 18
    assert isinstance(expr2, BinaryExpression)


def test_instance_field_access_returns_value_not_column() -> None:
    """实例访问字段应返回实际值，而不是 Column。"""
    from eorm.expression import Column as ColumnCls

    user = User(name="Alice", email="alice@example.com", age=30)
    assert user.name == "Alice"
    assert user.age == 30
    assert user.id is None
    assert not isinstance(user.name, ColumnCls)
    assert not isinstance(user.age, ColumnCls)


def test_column_only_fields_not_in_class_namespace() -> None:
    """字段名不应出现在类 __dict__ 中，只有 __columns__ 保存 Column。"""
    assert "name" not in User.__dict__
    assert "id" in User.__columns__
    assert User.__columns__["name"].field_name == "name"
