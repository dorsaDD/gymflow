"""Minimal in-memory stand-in for the real Flask-SQLAlchemy package.

This exists ONLY so GymFlow can be smoke-tested inside a sandbox that has no
package-index network access. It implements the narrow subset of the real
API that gymflow/models.py and gymflow/routes/*.py actually use (columns,
simple relationships/backrefs, filter/filter_by/order_by/join/get/get_or_404,
cascading delete-orphan). It is NOT shipped to the end user — the real
project depends on the genuine `Flask-SQLAlchemy` package declared in
requirements.txt, which works normally on any machine with internet access.

Do not use this file as a reference for how Flask-SQLAlchemy actually works;
it deliberately cuts corners that are safe only for this app's own test
scenarios.
"""
import itertools
import re

from flask import abort

_registry = {}          # class name -> Model subclass
_pending_backrefs = []  # (forward_relationship, backref_name)


class ColumnType:
    def __init__(self, *args, **kwargs):
        pass


Integer = ColumnType
String = ColumnType
Boolean = ColumnType
DateTime = ColumnType
Date = ColumnType
Text = ColumnType
Float = ColumnType


class ForeignKey:
    def __init__(self, target):
        self.target = target


class Expr:
    def __init__(self, fn):
        self._fn = fn

    def evaluate(self, obj):
        return self._fn(obj)


class OrderSpec:
    def __init__(self, name, reverse):
        self.name = name
        self.reverse = reverse


class InstrumentedColumn:
    def __init__(self, name):
        self.name = name

    def __eq__(self, other):
        return Expr(lambda obj: getattr(obj, self.name, None) == other)

    def __ge__(self, other):
        return Expr(lambda obj: getattr(obj, self.name, None) is not None
                     and getattr(obj, self.name) >= other)

    def __le__(self, other):
        return Expr(lambda obj: getattr(obj, self.name, None) is not None
                     and getattr(obj, self.name) <= other)

    def in_(self, values):
        values = list(values)
        return Expr(lambda obj: getattr(obj, self.name, None) in values)

    def asc(self):
        return OrderSpec(self.name, False)

    def desc(self):
        return OrderSpec(self.name, True)

    def __hash__(self):
        return hash(self.name)


class Column:
    _counter = itertools.count()

    def __init__(self, *args, primary_key=False, nullable=True, unique=False,
                 default=None, index=False):
        self.order = next(Column._counter)
        self.primary_key = primary_key
        self.nullable = nullable
        self.unique = unique
        self.default = default
        self.foreign_key = None
        for a in args:
            if isinstance(a, ForeignKey):
                self.foreign_key = a.target
        self.name = None

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, obj, objtype=None):
        if obj is None:
            return InstrumentedColumn(self.name)
        return obj.__dict__.get(self.name)

    def __set__(self, obj, value):
        obj.__dict__[self.name] = value


def _resolve_order(order_by):
    """Parses the "desc(Model.field)" / "asc(Model.field)" string convention
    used by models.py for relationship order_by=."""
    if not order_by or not isinstance(order_by, str):
        return None
    m = re.match(r"(asc|desc)\(\s*[A-Za-z_]+\.([A-Za-z_]+)\s*\)", order_by)
    if not m:
        return None
    direction, field = m.groups()
    return field, (direction == "desc")


class RelationshipProperty:
    def __init__(self, target, backref=None, uselist=None, cascade=None,
                 order_by=None, foreign_keys=None):
        self.target_name = target
        self.backref = backref
        self.uselist = uselist
        self.cascade = cascade or ""
        self.order_by = order_by
        self.foreign_keys = foreign_keys
        self.name = None
        self.owner = None

    def __set_name__(self, owner, name):
        # NOTE: __set_name__ fires from inside type.__new__, before
        # ModelMeta.__new__ has had a chance to set up cls.__relationships__,
        # so registration is deferred to ModelMeta.__new__ below.
        self.name = name
        self.owner = owner

    def _target_model(self):
        return _registry[self.target_name]

    def _fk_column_name(self):
        target = self._target_model()
        if self.foreign_keys:
            return self.foreign_keys.split(".")[-1]
        owner_table = self.owner.__tablename__
        candidates = [c.name for c in target.__columns__.values()
                      if c.foreign_key and c.foreign_key.startswith(owner_table + ".")]
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise RuntimeError(
                f"Ambiguous foreign key for relationship "
                f"{self.owner.__name__}.{self.name}: candidates={candidates}; "
                "pass foreign_keys=... to disambiguate."
            )
        return None

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        target = self._target_model()
        fk_name = self._fk_column_name()
        if fk_name is not None:
            rows = [r for r in target.__store__.values()
                    if getattr(r, fk_name, None) == obj.id]
            resolved = _resolve_order(self.order_by)
            if resolved:
                field, reverse = resolved
                rows = sorted(rows, key=lambda r: getattr(r, field), reverse=reverse)
            if self.uselist is False:
                return rows[0] if rows else None
            return rows
        # This side owns the FK itself (convention: relationship name + "_id")
        fk_attr = self.name + "_id"
        fk_value = getattr(obj, fk_attr, None)
        return target.__store__.get(fk_value)


class BackrefProperty:
    """The reverse ("belongs-to") side of a RelationshipProperty. Always
    resolves via the forward relationship's already-computed FK column."""

    def __init__(self, forward_rel):
        self._forward = forward_rel

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        owner_model = self._forward.owner
        fk_name = self._forward._fk_column_name()
        if fk_name is None:
            return None
        fk_value = getattr(obj, fk_name, None)
        return owner_model.__store__.get(fk_value)


def _attach_pending_backrefs():
    still_pending = []
    for forward_rel, backref_name in _pending_backrefs:
        target = _registry.get(forward_rel.target_name)
        if target is None:
            still_pending.append((forward_rel, backref_name))
            continue
        if backref_name not in target.__dict__:
            setattr(target, backref_name, BackrefProperty(forward_rel))
    _pending_backrefs[:] = still_pending


class ModelMeta(type):
    def __new__(mcs, name, bases, ns):
        cls = super().__new__(mcs, name, bases, ns)
        if name == "Model":
            return cls
        cls.__columns__ = {}
        cls.__relationships__ = []
        cls.__store__ = {}
        cls.__id_seq__ = itertools.count(1)
        for base in bases:
            if hasattr(base, "__columns__"):
                cls.__columns__.update(base.__columns__)
        for key, val in list(ns.items()):
            if isinstance(val, Column):
                cls.__columns__[key] = val
            elif isinstance(val, RelationshipProperty):
                cls.__relationships__.append(val)
                if val.backref:
                    _pending_backrefs.append((val, val.backref))
        _registry[name] = cls
        cls.query = QueryProperty(cls)
        _attach_pending_backrefs()
        return cls


_MISSING = object()


def _order_key(row, name):
    val = getattr(row, name, _MISSING)
    if val is not _MISSING:
        return val
    owner = getattr(row, "owner", None)
    if owner is not None:
        return getattr(owner, name, None)
    return None


class TextClause:
    """db.text(...) — resolved via a small fixed map of the exact strings
    this app happens to use for cross-relationship ordering."""

    _RESOLVERS = {
        "fitness_classes.schedule asc":
            lambda rows: sorted(rows, key=lambda b: b.fitness_class.schedule),
    }

    def __init__(self, text):
        self.text = text

    def apply(self, rows):
        resolver = self._RESOLVERS.get(self.text.strip())
        return resolver(rows) if resolver else rows


class Query:
    def __init__(self, model):
        self.model = model
        self._filters = []
        self._order = []

    def _clone(self):
        q = Query(self.model)
        q._filters = list(self._filters)
        q._order = list(self._order)
        return q

    def filter_by(self, **kwargs):
        q = self._clone()
        for k, v in kwargs.items():
            q._filters.append(lambda obj, k=k, v=v: getattr(obj, k, None) == v)
        return q

    def filter(self, *exprs):
        q = self._clone()
        for e in exprs:
            q._filters.append(e.evaluate)
        return q

    def join(self, *a, **k):
        return self

    def order_by(self, *specs):
        q = self._clone()
        q._order.extend(specs)
        return q

    def _base(self):
        return list(self.model.__store__.values())

    def all(self):
        rows = self._base()
        for f in self._filters:
            rows = [r for r in rows if f(r)]
        for spec in self._order:
            if isinstance(spec, OrderSpec):
                rows = sorted(rows, key=lambda r: (_order_key(r, spec.name) is None,
                                                     _order_key(r, spec.name) or ""),
                              reverse=spec.reverse)
            elif isinstance(spec, TextClause):
                rows = spec.apply(rows)
            elif isinstance(spec, InstrumentedColumn):
                rows = sorted(rows, key=lambda r: (_order_key(r, spec.name) is None,
                                                     _order_key(r, spec.name) or ""))
        return rows

    def first(self):
        rows = self.all()
        return rows[0] if rows else None

    def count(self):
        return len(self.all())

    def get(self, pk):
        return self.model.__store__.get(pk)

    def get_or_404(self, pk):
        obj = self.get(pk)
        if obj is None:
            abort(404)
        return obj


class QueryProperty:
    def __init__(self, model):
        self.model = model

    def __get__(self, obj, objtype=None):
        return Query(self.model)


class Model(metaclass=ModelMeta):
    def __init__(self, **kwargs):
        for name, col in type(self).__columns__.items():
            if name in kwargs:
                self.__dict__[name] = kwargs[name]
            else:
                self.__dict__[name] = col.default() if callable(col.default) else col.default
        for k, v in kwargs.items():
            if k not in type(self).__columns__:
                setattr(self, k, v)


class SessionManager:
    def __init__(self):
        self._pending_add = []

    def add(self, obj):
        self._pending_add.append(obj)

    def _assign_ids(self):
        for obj in self._pending_add:
            cls = type(obj)
            if obj.id is None:
                obj.id = next(cls.__id_seq__)
            cls.__store__[obj.id] = obj
        self._pending_add = []

    def flush(self):
        self._assign_ids()

    def commit(self):
        self._assign_ids()

    def rollback(self):
        self._pending_add = []

    def delete(self, obj):
        self._cascade_delete(obj)

    def _cascade_delete(self, obj):
        cls = type(obj)
        for rel in cls.__relationships__:
            if "delete-orphan" in (rel.cascade or ""):
                children = rel.__get__(obj)
                if children is None:
                    continue
                if not isinstance(children, list):
                    children = [children]
                for child in list(children):
                    self._cascade_delete(child)
        store = cls.__store__
        if getattr(obj, "id", None) in store:
            del store[obj.id]


class SQLAlchemy:
    def __init__(self):
        self.session = SessionManager()
        self.Model = Model
        self.Column = Column
        self.Integer = Integer
        self.String = String
        self.Boolean = Boolean
        self.DateTime = DateTime
        self.Date = Date
        self.Text = Text
        self.Float = Float
        self.ForeignKey = ForeignKey
        self.relationship = RelationshipProperty
        self.text = TextClause

    def init_app(self, app):
        pass

    def create_all(self):
        pass
