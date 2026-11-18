"""SQLGlot 检查与认证模板 AST 比对；任意模型 SQL 不是本版本的可执行入口。"""

import sqlite3
import time
from contextlib import closing
from pathlib import Path

from sqlglot import exp, parse, parse_one

from credit.catalog import Catalog
from credit.models import MetricQuery, User
from credit.security import AccessDenied


class SQLRejected(ValueError):
    pass


def compile_query(request: MetricQuery, user: User, catalog: Catalog) -> str:
    metric = catalog.metric(request.metric_id, user, "query")
    if request.data_status == "REALTIME" and not metric["realtime"]:
        raise SQLRejected("该指标未认证实时来源")
    if metric["time_kind"] == "stock" and request.start_date != request.end_date and "biz_date" not in request.group_by:
        raise SQLRejected("时点指标跨日查询必须保留 biz_date，不能累加每日余额")
    table = "ads_credit_daily" if request.data_status == "FINAL" else "ads_credit_realtime"
    # The certified catalog belongs to credit. Broader user grants must not widen
    # the data scope of this metric.
    if "credit" not in user.domains:
        raise AccessDenied("无可访问的数据域")
    domains = exp.Literal.string("credit").sql(dialect="sqlite")
    columns = [*request.group_by, f"{metric['expression']} AS value"]
    sql = (f"SELECT {', '.join(columns)} FROM {table} "
           f"WHERE biz_date BETWEEN '{request.start_date}' AND '{request.end_date}' "
           f"AND domain IN ({domains}) AND data_status = '{request.data_status}'")
    if request.group_by:
        sql += f" GROUP BY {', '.join(request.group_by)} ORDER BY {', '.join(request.group_by)}"
    return parse_one(sql, read="sqlite").limit(request.max_rows).sql(dialect="sqlite")


def guard(candidate: str, request: MetricQuery, user: User, catalog: Catalog) -> str:
    try:
        expressions = parse(candidate, read="sqlite")
    except Exception as exc:
        raise SQLRejected("SQL 无法解析") from exc
    if len(expressions) != 1 or not isinstance(expressions[0], exp.Select):
        raise SQLRejected("只允许单条 SELECT")
    tree = expressions[0]
    if tree.find(exp.Star) or tree.find(exp.Join) or tree.find(exp.Subquery) or tree.args.get("with_"):
        raise SQLRejected("认证模板不允许星号、JOIN、子查询或 CTE")
    # Structural equality covers tables, columns, functions, filters, OR bypasses,
    # aggregation, time boundaries and LIMIT. No 'filter exists somewhere' shortcut.
    expected = parse_one(compile_query(request, user, catalog), read="sqlite")
    if tree != expected:
        raise SQLRejected("SQL 超出认证指标模板或授权范围")
    return expected.sql(dialect="sqlite")


def execute(db: Path, candidate: str, request: MetricQuery, user: User, catalog: Catalog,
            timeout_seconds: float = 2.0) -> list[dict]:
    sql = guard(candidate, request, user, catalog)
    started = time.monotonic()
    uri = db.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=timeout_seconds)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.set_progress_handler(lambda: int(time.monotonic() - started > timeout_seconds), 100)
        return [dict(row) for row in conn.execute(sql).fetchmany(request.max_rows)]
