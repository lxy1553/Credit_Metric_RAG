"""Read-only, in-memory trial calculations for reviewed draft metric candidates."""

import calendar
import json
import re
import sqlite3
import time
from contextlib import closing
from datetime import date
from pathlib import Path
from uuid import uuid4

from sqlglot import exp, parse_one

from credit.models import ExplorationQuery, Result, User

EXPLORATION_PREFIXES = ("探索试算", "非认证试算")
EXPLORATION_OPT_IN_PHRASES = (
    "隔离探索模式下试算", "探索模式下试算", "请使用探索模式试算", "请在探索模式下试算",
    "请在隔离探索模式下试算", "帮我探索试算", "在隔离探索模式试算",
)
SANDBOX_EXPRESSIONS = {
    "application_rejection_rate": (
        "1.0 * SUM(application_count - approved_count) / NULLIF(SUM(application_count), 0)"
    ),
    "vintage_overdue_rate": (
        "1.0 * SUM(CASE WHEN overdue_days > 3 THEN overdue_principal_cents ELSE 0 END) "
        "/ NULLIF(SUM(disbursed_amount_cents), 0)"
    ),
}
MAX_SOURCE_ROWS = 1000


def is_exploration_prompt(question: str) -> bool:
    normalized = question.strip()
    return (normalized.startswith(EXPLORATION_PREFIXES)
            or any(phrase in normalized for phrase in EXPLORATION_OPT_IN_PHRASES))


class ExplorationService:
    def __init__(self, db: Path):
        self.db = Path(db)
        document = json.loads((Path(__file__).parent / "data/exploration_metrics.json").read_text())
        self.version = document["version"]
        self.updated_at = document["updated_at"]
        self.candidates = {item["id"]: item for item in document["candidates"]}

    def match(self, question: str) -> dict | None:
        folded = question.casefold()
        matches = [candidate for candidate in self.candidates.values()
                   if any(alias.casefold() in folded
                          for alias in [candidate["name"], *candidate["aliases"]])]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def authorize(user: User) -> bool:
        return "analyst" in user.roles and "credit" in user.domains

    def _result(self, status: str, message: str, *, metric_id: str | None = None,
                rows: list[dict] | None = None, sql: str | None = None,
                warnings: list[str] | None = None, citations: list[dict] | None = None) -> Result:
        return Result(
            status=status, intent="exploration", message=message, trace_id=uuid4().hex,
            metric_id=metric_id, data_status="EXPLORATORY", rows=rows or [], sql=sql,
            citations=citations or [],
            warnings=warnings or [], certification_status="NON_CERTIFIED",
            candidate_version=self.version,
            candidate_updated_at=self.updated_at,
            steps=["draft_route", "authorize", "sandbox", "validate", "audit"],
        )

    def from_question(self, question: str, user: User) -> Result:
        if not self.authorize(user):
            return self._result("refused", "当前身份没有非认证探索试算权限。")
        candidate = self.match(question)
        if candidate is None:
            return self._result("refused", "未找到唯一的探索候选指标；请先登记候选口径与负责人。")
        if candidate["id"] == "vintage_overdue_rate":
            date_range = _vintage_snapshot_range(question)
            if date_range is None:
                return self._result("needs_clarification", "请提供一个观察月份或月份范围，如 202606 或 2025年1-12月；"
                                    "系统按月末 FINAL 合成快照做非认证 Vintage 试算。",
                                    metric_id=candidate["id"])
            start_date, end_date = date_range
            request = ExplorationQuery(
                metric_id=candidate["id"], start_date=start_date, end_date=end_date,
                group_by=["cohort_month", "mob"], accept_non_certified=True,
            )
            return self.run(request, user)
        dates = re.findall(r"\d{4}-\d{2}-\d{2}", question)
        if not dates or len(dates) > 2:
            return self._result("needs_clarification", "请提供明确的业务日期或日期范围（最长 31 天）。",
                                metric_id=candidate["id"])
        if any(term in question for term in ("实时", "REALTIME")):
            return self._result("refused", "探索试算目前仅使用已发布的合成离线数据。",
                                metric_id=candidate["id"])
        try:
            request = ExplorationQuery(
                metric_id=candidate["id"], start_date=date.fromisoformat(dates[0]),
                end_date=date.fromisoformat(dates[-1]),
                group_by=(["biz_date", "product"] if "按产品" in question or "各产品" in question
                          else [] if "汇总" in question or "合计" in question else ["biz_date"]),
                accept_non_certified=True,
            )
        except ValueError:
            return self._result("needs_clarification", "日期无效或超过 31 天，请重新指定探索范围。",
                                metric_id=candidate["id"])
        return self.run(request, user)

    def run(self, request: ExplorationQuery, user: User) -> Result:
        if not self.authorize(user):
            return self._result("refused", "当前身份没有非认证探索试算权限。")
        candidate = self.candidates.get(request.metric_id)
        if candidate is None:
            return self._result("refused", "该指标未登记为探索候选；请先补充业务口径并发起治理工单。")
        if candidate["status"] == "BLOCKED_SOURCE":
            missing = "、".join(candidate.get("missing_fields", []))
            return self._result(
                "unavailable",
                f"{candidate['name']}属于非认证候选，缺少{missing}及经确认的分母口径，当前无法试算。"
                "请业务和数据负责人补齐候选口径与来源字段；本次没有创建工单。",
                metric_id=candidate["id"],
            )
        if candidate["status"] != "DRAFT" or candidate.get("aggregation") not in SANDBOX_EXPRESSIONS:
            return self._result("refused", "候选指标尚无经过代码审查的试算模板。",
                                metric_id=candidate["id"])
        allowed_fields = {
            "ads_credit_daily": {"application_count", "approved_count"},
            "exploration_vintage_snapshot": {
                "snapshot_date", "cohort_month", "disburse_date", "mob", "overdue_days",
                "overdue_principal_cents", "disbursed_amount_cents", "domain", "data_status",
            },
        }
        if (candidate.get("source_dataset") not in allowed_fields
                or set(candidate.get("source_fields", [])) != allowed_fields[candidate["source_dataset"]]):
            return self._result("refused", "候选指标的数据来源或字段超出探索沙箱白名单。",
                                metric_id=candidate["id"])
        try:
            source_rows = self._read_source(request, user, candidate)
            if len(source_rows) > MAX_SOURCE_ROWS:
                return self._result("unavailable", "探索范围超过沙箱源记录上限，请缩短日期范围。",
                                    metric_id=candidate["id"])
            if not source_rows:
                return self._result("no_data", "探索范围内没有可访问的已发布合成数据。",
                                    metric_id=candidate["id"])
            sql = self._compile(request, candidate)
            rows = self._execute_sandbox(source_rows, sql, request.max_rows, candidate)
        except (sqlite3.Error, ValueError):
            return self._result("unavailable", "探索沙箱或数据源不可用，本次未发布结果。",
                                metric_id=candidate["id"])
        if not rows or all(row["value"] is None for row in rows):
            return self._result("no_data", "分母为零或无可用结果，本次不返回试算数值。",
                                metric_id=candidate["id"], sql=sql)
        citations = []
        if candidate["id"] == "vintage_overdue_rate":
            citations = [
                {"source": "credit/data/exploration_metrics.json",
                 "locator": "#/candidates[id=vintage_overdue_rate]"},
                {"source": "本地合成表 exploration_vintage_snapshot（非生产来源）",
                 "locator": (f"snapshot_date={request.start_date}..{request.end_date}; "
                             "domain=credit; data_status=FINAL")},
            ]
        return self._result(
            "ok", f"非认证试算：{candidate['name']}。候选口径：{candidate['formula']}。"
            "本结果使用日历月差计算 MOB，按 overdue_days > 3 识别逾期本金。"
            "仅供业务核对，不可用于正式报表；请提交指标建设/治理工单完成认证。",
            metric_id=candidate["id"], rows=rows, sql=sql,
            citations=citations,
            warnings=["来源为授权范围内的合成演示 FINAL 分区；源数据已发布不代表候选指标已认证。"],
        )

    def _read_source(self, request: ExplorationQuery, user: User, candidate: dict) -> list[tuple]:
        domains = sorted(user.domains.intersection({"credit"}))
        placeholders = ", ".join("?" for _ in domains)
        if candidate["id"] == "vintage_overdue_rate":
            sql = ("SELECT snapshot_date, cohort_month, mob, domain, overdue_days, "
                   "overdue_principal_cents, disbursed_amount_cents "
                   "FROM exploration_vintage_snapshot WHERE snapshot_date BETWEEN ? AND ? "
                   f"AND domain IN ({placeholders}) AND data_status='FINAL' LIMIT ?")
            parameters = (str(request.start_date), str(request.end_date), *domains, MAX_SOURCE_ROWS + 1)
        else:
            sql = ("SELECT biz_date, product, domain, application_count, approved_count "
                   "FROM ads_credit_daily WHERE biz_date BETWEEN ? AND ? "
                   f"AND domain IN ({placeholders}) AND data_status='FINAL' "
                   "LIMIT ?")
            parameters = (str(request.start_date), str(request.end_date), *domains, MAX_SOURCE_ROWS + 1)
        uri = self.db.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=2.0)) as conn:
            conn.execute("PRAGMA query_only=ON")
            started = time.monotonic()
            conn.set_progress_handler(lambda: int(time.monotonic() - started > 2.0), 100)
            return conn.execute(sql, parameters).fetchall()

    @staticmethod
    def _compile(request: ExplorationQuery, candidate: dict) -> str:
        if candidate["source_dataset"] == "exploration_vintage_snapshot":
            if set(request.group_by) != {"cohort_month", "mob"}:
                raise ValueError("Vintage 试算必须按 cohort_month 与 mob 分组")
            source = "sandbox_vintage"
            columns = "cohort_month, mob, "
            where = (f"snapshot_date BETWEEN '{request.start_date}' "
                     f"AND '{request.end_date}'")
            grouping = "cohort_month, mob"
            numerator = "SUM(CASE WHEN overdue_days > 3 THEN overdue_principal_cents ELSE 0 END)"
            denominator = "SUM(disbursed_amount_cents)"
            expression = (f"1.0 * {numerator} / NULLIF({denominator}, 0) AS value, "
                          f"{numerator} AS overdue_principal_cents, "
                          f"{denominator} AS disbursed_amount_cents")
            sql = f"SELECT {columns}{expression} FROM {source} WHERE {where}"
        else:
            grouping = ", ".join(request.group_by)
            columns = f"{grouping}, " if grouping else ""
            sql = (f"SELECT {columns}{SANDBOX_EXPRESSIONS[candidate['aggregation']]} AS value "
                   "FROM sandbox_daily "
                   f"WHERE biz_date BETWEEN '{request.start_date}' AND '{request.end_date}'")
        if grouping:
            sql += f" GROUP BY {grouping} ORDER BY {grouping}"
        sql += f" LIMIT {request.max_rows}"
        parsed = parse_one(sql, read="sqlite")
        expected_table = ("sandbox_vintage" if candidate["source_dataset"] == "exploration_vintage_snapshot"
                          else "sandbox_daily")
        tables = {table.name for table in parsed.find_all(exp.Table)}
        if (not isinstance(parsed, exp.Select) or parsed.find(exp.Join) or parsed.find(exp.Subquery)
                or tables != {expected_table}):
            raise ValueError("探索模板不是允许的单表查询")
        return parsed.sql(dialect="sqlite")

    @staticmethod
    def _execute_sandbox(source_rows: list[tuple], sql: str, max_rows: int,
                         candidate: dict) -> list[dict]:
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.row_factory = sqlite3.Row
            if candidate["source_dataset"] == "exploration_vintage_snapshot":
                conn.execute("""CREATE TABLE sandbox_vintage (
                    snapshot_date TEXT NOT NULL, cohort_month TEXT NOT NULL, mob INTEGER NOT NULL,
                    domain TEXT NOT NULL, overdue_days INTEGER NOT NULL,
                    overdue_principal_cents INTEGER NOT NULL,
                    disbursed_amount_cents INTEGER NOT NULL)""")
                conn.executemany("INSERT INTO sandbox_vintage VALUES(?,?,?,?,?,?,?)", source_rows)
            else:
                conn.execute("""CREATE TABLE sandbox_daily (
                    biz_date TEXT NOT NULL, product TEXT NOT NULL, domain TEXT NOT NULL,
                    application_count INTEGER NOT NULL, approved_count INTEGER NOT NULL)""")
                conn.executemany("INSERT INTO sandbox_daily VALUES(?,?,?,?,?)", source_rows)
            conn.execute("PRAGMA query_only=ON")
            started = time.monotonic()
            conn.set_progress_handler(lambda: int(time.monotonic() - started > 2.0), 100)
            return [dict(row) for row in conn.execute(sql).fetchmany(max_rows)]


def _vintage_snapshot_range(question: str) -> tuple[date, date] | None:
    range_match = re.search(
        r"(?<!\d)(20\d{2})\s*年?\s*(0?[1-9]|1[0-2])\s*[-至到~～]\s*(0?[1-9]|1[0-2])\s*月",
        question,
    )
    if range_match:
        year, first_month, last_month = map(int, range_match.groups())
        if first_month > last_month:
            return None
        return (
            date(year, first_month, calendar.monthrange(year, first_month)[1]),
            date(year, last_month, calendar.monthrange(year, last_month)[1]),
        )

    values = re.findall(r"(?<!\d)(\d{4}-\d{2}-\d{2}|\d{4}-\d{2}|\d{6})(?!\d)", question)
    if not values or len(values) > 2:
        return None
    result = []
    for value in values:
        try:
            if re.fullmatch(r"\d{6}", value):
                year, month = int(value[:4]), int(value[4:])
                result.append(date(year, month, calendar.monthrange(year, month)[1]))
            elif re.fullmatch(r"\d{4}-\d{2}", value):
                year, month = map(int, value.split("-"))
                result.append(date(year, month, calendar.monthrange(year, month)[1]))
            else:
                result.append(date.fromisoformat(value))
        except ValueError:
            return None
    return result[0], result[-1]
