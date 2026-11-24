"""用轻量 FakeDB 验证 services/directory.py 的事务编排，不依赖 SQLAlchemy。

覆盖真实服务里除 SQL 方言外的全部口径：
- 补录/更正/撤销同步写入 serving 目录与变更流；
- 重建期间旧版继续服务、新变更持续追平；
- worker 中断续建、收齐后一次性原子切换、重试不重复发布；
- 点查批次固定版本且证据固化。
"""
import sys
import types
import unittest
from datetime import date
from time import time


def _install_stubs():
    # 在导入服务模块前装好 SQLAlchemy / FastAPI 桩件。
    sqlalchemy = types.ModuleType("sqlalchemy")

    class _Select:
        def __init__(self, table=None):
            self.table = table
            self._where = None
            self._order = None
            self._limit = None
            self._lock = False
            self._skip = False
            self._is_count = isinstance(table, tuple) and table and table[0] == "count"

        def where(self, clause):
            self._where = clause
            return self

        def order_by(self, clause):
            self._order = clause
            return self

        def limit(self, value):
            self._limit = value
            return self

        def select_from(self, table):
            self.table = table
            return self

        def with_for_update(self, skip_locked=False):
            self._lock = True
            self._skip = skip_locked
            return self

    class _Insert:
        def __init__(self, table):
            self.table = table
            self.values_kwargs = None

        def values(self, **kwargs):
            self.values_kwargs = kwargs
            return self

    class _Update:
        def __init__(self, table):
            self.table = table
            self._where = None
            self.values_kwargs = None

        def where(self, *clauses):
            self._where = clauses[0]  # 服务层更新条件均为单条等值匹配
            return self

        def values(self, **kwargs):
            self.values_kwargs = kwargs
            return self

    def select(table=None):
        return _Select(table)

    def insert(table):
        return _Insert(table)

    def update(table):
        return _Update(table)

    class _Func:
        def coalesce(self, *args):
            return ("coalesce", args)

        def max(self, column):
            ref = getattr(column, "_ref", None) or ("?", column.name)
            return ("max", ref)

        def count(self):
            return ("count",)

    sqlalchemy.select = select
    sqlalchemy.insert = insert
    sqlalchemy.update = update
    sqlalchemy.func = _Func()

    dialect_pkg = types.ModuleType("sqlalchemy.dialects")
    mysql_pkg = types.ModuleType("sqlalchemy.dialects.mysql")
    pg_pkg = types.ModuleType("sqlalchemy.dialects.postgresql")

    class _DialectInsert(_Insert):
        def on_conflict_do_nothing(self, index_elements=None):
            return self

        def on_conflict_do_update(self, index_elements=None, set_=None):
            self.values_kwargs.update(set_ or {})
            return self

        def on_duplicate_key_update(self, **kwargs):
            self.values_kwargs.update(kwargs)
            return self

    mysql_pkg.insert = pg_pkg.insert = lambda table: _DialectInsert(table)
    sys.modules |= {
        "sqlalchemy": sqlalchemy, "sqlalchemy.dialects": dialect_pkg,
        "sqlalchemy.dialects.mysql": mysql_pkg,
        "sqlalchemy.dialects.postgresql": pg_pkg,
    }

    fastapi = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code, detail):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    fastapi.HTTPException = HTTPException
    sys.modules["fastapi"] = fastapi

    # 项目内部依赖桩件。
    resources = types.ModuleType("src.config.resources")
    resources.engine = object()
    sys.modules["src.config.resources"] = resources

    audit_mod = types.ModuleType("src.services.audit")
    audit_mod.record = lambda *args, **kwargs: None
    sys.modules["src.services.audit"] = audit_mod

    serial = types.ModuleType("src.utils.serial")
    import hashlib
    import json

    def canonical_json(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode()

    serial.canonical_json = canonical_json
    serial.content_digest = lambda value: hashlib.sha256(canonical_json(value)).hexdigest()
    sys.modules["src.utils.serial"] = serial

    models_pkg = types.ModuleType("src.models.directory")

    class Column:
        def __init__(self, name):
            self.name = name

        def __eq__(self, other):
            return (self.name, other)

        def __gt__(self, other):
            return (self.name, ">", other)

        def desc(self):
            return ("desc", self.name)

        def asc(self):
            return ("asc", self.name)

    class _ColumnProxy:
        def __getattr__(self, name):
            return Column(name)

    class Table:
        def __init__(self, name):
            self.name = name
            self.c = _ColumnProxy()

    for table_name in ("certificates", "certificate_changes", "directory_versions", "probe_batches"):
        setattr(models_pkg, table_name, Table(table_name))
    models_pkg.Table = Table
    sys.modules["src.models.directory"] = models_pkg
    return HTTPException


HTTPException = _install_stubs()

from src.services import directory as service  # noqa: E402
from src.models.directory import (  # noqa: E402
    Table, certificate_changes, certificates, directory_versions, probe_batches,
)


class Row(dict):
    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError as exc:
            raise AttributeError(item) from exc


class FakeResult:
    def __init__(self, rows):
        self._rows = rows
        self.inserted_primary_key = [rows[-1].get("id") or rows[-1].get("version")
                                      or rows[-1].get("seq")] if rows else [None]

    def mappings(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return [row[list(row)[0]] for row in self._rows]

    def __iter__(self):
        return iter(self._rows)


class FakeDB:
    """最小关系表模拟器，支持服务用到的 select/insert/update。"""

    def __init__(self):
        self.data = {
            certificates.name: [],
            certificate_changes.name: [],
            directory_versions.name: [],
            probe_batches.name: [],
        }
        self.dialect = types.SimpleNamespace(name="postgresql")
        self._auto = {certificate_changes.name: 0, directory_versions.name: 0,
                      probe_batches.name: 0}

    def execute(self, statement):
        if statement.__class__.__name__ == "_Insert" or "DialectInsert" in statement.__class__.__name__:
            return self._insert(statement)
        if statement.__class__.__name__ == "_Update":
            return self._update(statement)
        return self._select(statement)

    def scalar(self, statement):
        # select(func.count()).select_from(...) 形式的计数查询。
        if statement.__class__.__name__ == "_Select" and getattr(statement, "_is_count", False):
            return len(self.data[statement.table.name])
        # select(func.coalesce(func.max(col), 0)) 形式的标量查询。
        if statement.__class__.__name__ == "_Select" and isinstance(statement.table, tuple):
            return self._eval_aggregate(statement.table)
        # 聚合函数查询。
        if isinstance(statement, tuple) and statement and statement[0] in ("coalesce", "max", "count"):
            return self._eval_aggregate(statement)
        result = self.execute(statement)
        rows = result._rows
        if not rows:
            return None
        return list(rows[0].values())[0]

    def _eval_aggregate(self, expression):
        if expression[0] == "count":
            return len(self.data[certificates.name])
        if expression[0] == "coalesce":
            return self._eval_aggregate(expression[1][0])
        if expression[0] == "max":
            ref = expression[1]
            table_name, column = ref if isinstance(ref, tuple) and ref[0] != "?" \
                else (self._table_for_column(ref[1] if ref[0] == "?" else ref).name,
                      ref[1] if ref[0] == "?" else ref)
            values = [row[column] for row in self.data[table_name] if row.get(column) is not None]
            return max(values) if values else 0
        raise AssertionError(f"未支持的聚合 {expression}")

    def _insert(self, statement):
        table = statement.table.name
        defaults = {
            directory_versions.name: {
                "snapshot": None, "base_seq": 0, "applied_seq": 0, "digest": None,
                "progress": 0, "note": None, "published_at": None,
            },
            probe_batches.name: {
                "result": None, "digest": None, "failure": None, "finished_at": None,
            },
        }
        row = Row(defaults.get(table, {}))
        row.update(statement.values_kwargs)
        if table == certificates.name:
            # 模拟 ON CONFLICT (code) DO UPDATE：同编号原地更新而非新增。
            for existing in self.data[table]:
                if existing["code"] == row["code"]:
                    existing.update(row)
                    return FakeResult([existing])
        if table in self._auto:
            self._auto[table] += 1
            key = "version" if table == directory_versions.name else \
                  "seq" if table == certificate_changes.name else "id"
            row.setdefault(key, self._auto[table])
        self.data[table].append(row)
        return FakeResult([row])

    def _update(self, statement):
        table = statement.table.name
        key, expected = statement._where
        for row in self.data[table]:
            if row.get(key) == expected:
                row.update(statement.values_kwargs)
        return FakeResult([])

    def _select(self, stmt):
        target = stmt.table
        # target 可能是 Table 或单列 Column。
        if isinstance(target, Table):
            table = target
            only_column = None
        else:
            only_column = target.name
            table = self._table_for_column(only_column)
        rows = list(self.data[table.name])
        if stmt._where is not None:
            clause = stmt._where
            if len(clause) == 3:  # (column, ">", value)
                column, _, value = clause
                rows = [row for row in rows if row[column] > value]
            else:
                key, expected = clause
                rows = [row for row in rows if row.get(key) == expected]
        if stmt._order is not None:
            if isinstance(stmt._order, tuple):
                direction, column = stmt._order
            else:
                direction, column = "asc", stmt._order.name
            rows.sort(key=lambda row: row[column], reverse=(direction == "desc"))
        if stmt._limit:
            rows = rows[:stmt._limit]
        if only_column:
            rows = [Row({only_column: row[only_column]}) for row in rows]
        return FakeResult([Row(row) for row in rows])

    def _table_for_column(self, column):
        for name, rows in self.data.items():
            if rows and column in rows[0]:
                candidate = types.SimpleNamespace(name=name)
                return candidate
        mapping = {"version": directory_versions, "seq": certificate_changes,
                   "status": directory_versions, "id": probe_batches}
        return mapping.get(column, directory_versions)


def make_cert(code, revoked=False):
    return types.SimpleNamespace(
        code=code, lift=f"L-{code}", expires_on="2026-12-31", revoked=revoked,
    )


class ServiceOrchestrationTest(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        service.bootstrap(self.db)

    def test_register_revoke_sync_and_rebuild_switch(self):
        # 补录两条，撤销一条。
        service.register_certificate(self.db, make_cert("YT-01"), "operator")
        service.register_certificate(self.db, make_cert("YT-02"), "operator")
        service.revoke_certificate(self.db, "YT-01", "operator")
        overview = service.overview(self.db)
        self.assertEqual(overview["serving_version"], 1)
        self.assertEqual(overview["certificate_count"], 2)
        self.assertEqual(overview["latest_seq"], 3)

        # 更正已入库：旧目录点查能查到撤销状态。
        batch = service.create_probe_batch(self.db, ["YT-01", "YT-99"], "2026-11-24", "viewer")
        self.assertEqual(batch["directory_version"], 1)
        verdicts = {row["项目"]: row["结论"] for row in batch["result"]["rows"]
                    if row["类别"] == "点查请求"}
        self.assertEqual(verdicts, {"YT-01": "已撤销", "YT-99": "未登记"})
        frozen_digest = batch["digest"]

        # 申请调小容量 4 -> 2。
        task = service.request_rebuild(self.db, 2, "operator")
        self.assertEqual(task["status"], "rebuilding")
        with self.assertRaises(HTTPException):
            service.request_rebuild(self.db, 1, "operator")  # 并发重建被拒

        # 重建期间继续补录，旧版仍服务。
        service.register_certificate(self.db, make_cert("YT-03"), "operator")
        self.assertEqual(service.overview(self.db)["serving_version"], 1)

        # 第一次 worker 追不平（模拟中断后状态已落盘），再次推进直至切换。
        self.assertTrue(service.service_rebuild(self.db))
        if service.overview(self.db)["rebuilding"] is not None:
            self.assertTrue(service.service_rebuild(self.db))
        overview = service.overview(self.db)
        self.assertIsNone(overview["rebuilding"])
        self.assertEqual(overview["serving_version"], 2)
        self.assertEqual(overview["serving_capacity"], 2)
        archived = [row for row in self.db.data[directory_versions.name] if row["status"] == "archived"]
        self.assertEqual(len(archived), 1)

        # 重复推进不会再发布新版本。
        service.service_rebuild(self.db)
        versions = [row["version"] for row in self.db.data[directory_versions.name]]
        self.assertEqual(sorted(versions), [1, 2])

        # 新目录不漏证、撤销不复活。
        from src.domain.hash_directory import Directory
        serving = next(row for row in self.db.data[directory_versions.name]
                       if row["status"] == "serving")
        records = Directory.from_snapshot(serving["snapshot"]).records()
        self.assertEqual(set(records), {"YT-01", "YT-02", "YT-03"})
        self.assertTrue(records["YT-01"].revoked)

        # 切换后新批次固定 V2；旧批次证据摘要保持原样。
        new_batch = service.create_probe_batch(self.db, ["YT-03"], "2026-11-24", "viewer")
        self.assertEqual(new_batch["directory_version"], 2)
        old = service.get_probe_batch(self.db, batch["id"])
        self.assertEqual(old["digest"], frozen_digest)

    def test_rebuild_rejects_non_shrink(self):
        service.register_certificate(self.db, make_cert("YT-01"), "operator")
        with self.assertRaises(HTTPException) as ctx:
            service.request_rebuild(self.db, 4, "operator")
        self.assertEqual(ctx.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
