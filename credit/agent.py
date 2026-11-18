import hashlib
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from credit import traces
from credit.catalog import Catalog
from credit.exploration import ExplorationService, is_exploration_prompt
from credit.intent_router import DeepSeekIntentRouter
from credit.memory import StatelessMemory, TaskState, session_key
from credit.models import ExplorationQuery, MetricQuery, Question, Result, ResultSection, User
from credit.response_cache import NullResultCache, cache_key, data_cache_key
from credit.retrieval import LocalRetrieval
from credit.security import AccessDenied, authorize
from credit.sql_guard import SQLRejected, compile_query, execute
from credit.warehouse import connect, now

_FOLLOWUP_PATTERN = re.compile(
    r"[\s，那呢？?。]*(?:(?:那|按产品|各产品|按渠道|按天|按日期|按日|汇总|合计|实时|REALTIME|离线|正式|FINAL|口径|定义|怎么算|怎么计算|来源|血缘|上游表|字段|今天|昨天|明天|前天|最近|本周|上周|本月|上月|今年|呢|的|从|到|至|和|查询|日期|时间)|[\d\s,，?？。:\-])+",
    re.IGNORECASE,
)
_RELATIVE_TIME_TERMS = ("今天", "昨天", "上月", "最近", "本月", "今年", "上周", "上季度")
_FINAL_QUALITY_CHECKS = frozenset({
    "applications_count", "approved_count", "loans_count", "loan_balance",
    "overdue_balance", "repayment_amount",
})

_NUMBER = r"(\d{1,12}(?:\.\d{1,4})?)"
_MONEY_UNIT = r"(亿元|万元|亿|万|元)"
_MONEY_SCALE = {"元": 1, "万": 10_000, "万元": 10_000,
                "亿": 100_000_000, "亿元": 100_000_000}


def _one_operand(question: str, patterns: tuple[str, ...], *, money: bool = False) -> Decimal | None:
    values = []
    for pattern in patterns:
        for match in re.finditer(pattern, question):
            amount = Decimal(match.group(1))
            if money:
                amount *= _MONEY_SCALE[match.group(2)]
            values.append(amount)
    return values[0] if values and all(value == values[0] for value in values) else None


def _supplied_ratio(question: str, metric_id: str | None) -> tuple[Decimal, Decimal] | None:
    """Extract only explicitly labelled operands for two certified ratio formulas."""
    if metric_id == "approval_rate":
        numerator = _one_operand(question, (
            rf"(?:通过|批准|审批通过)\s*{_NUMBER}\s*笔",
            rf"{_NUMBER}\s*笔\s*(?:通过|批准)(?!率)",
        ))
        denominator = _one_operand(question, (
            rf"{_NUMBER}\s*笔\s*(?:申请|进件)",
            rf"(?:总申请|申请总数|申请笔数|申请共|进件总数)\s*(?:为|是|有)?\s*{_NUMBER}\s*笔",
        ))
    elif metric_id == "overdue_rate":
        numerator = _one_operand(question, (
            rf"(?:逾期本金|逾期金额)\s*(?:为|是)?\s*{_NUMBER}\s*{_MONEY_UNIT}",
            rf"{_NUMBER}\s*{_MONEY_UNIT}\s*(?:逾期本金|逾期金额)",
        ), money=True)
        denominator = _one_operand(question, (
            rf"(?:剩余本金|贷款余额|在贷余额)\s*(?:为|是)?\s*{_NUMBER}\s*{_MONEY_UNIT}",
            rf"{_NUMBER}\s*{_MONEY_UNIT}\s*(?:剩余本金|贷款余额|在贷余额)",
        ), money=True)
    else:
        return None
    return (numerator, denominator) if numerator is not None and denominator is not None else None


def _calculation_metric(question: str, catalog: Catalog) -> str | None:
    # Numerator/denominator names may themselves be certified metrics. Select
    # the explicitly named ratio, but never select one of two ratio targets.
    ratios = {metric["id"] for metric in catalog.metrics.values()
              if metric["unit"] == "比例" and any(
                  alias in question for alias in (metric["name"], *metric["aliases"]))}
    return next(iter(ratios)) if len(ratios) == 1 else None


def _contextual_followup_intents(question: str, previous: TaskState, catalog: Catalog):
    """Route only clear, short ellipses from the current session; never let LLM guess context."""
    if catalog.match(question) or not _FOLLOWUP_PATTERN.fullmatch(question):
        return None
    if not previous.metric_id:
        return []
    if any(term in question for term in ("口径", "定义", "怎么算", "怎么计算", "如何计算")):
        return ["definition"]
    if any(term in question for term in ("血缘", "来源", "上游表")):
        return ["lineage"]
    if "字段" in question:
        return ["field"]
    if any(term in question for term in (
        "按产品", "各产品", "按渠道", "按天", "按日期", "按日", "汇总", "合计",
        "实时", "REALTIME", "离线", "正式", "FINAL", "查询", "多少",
        *_RELATIVE_TIME_TERMS,
    )) or re.search(r"\d{4}-\d{2}-\d{2}", question):
        return ["query"]
    return []


class WorkflowState(TypedDict, total=False):
    question: str
    task: TaskState
    resolver: object
    intent: str
    result: Result
    steps: list[str]
    trace_id: str
    allow_data_cache: bool
    forced_intent: str


def _unregistered_metric_message(question: str, intent: str,
                                 exploration: ExplorationService | None = None) -> str:
    if re.search(r"\bvintage\b", question, re.IGNORECASE):
        if intent == "definition":
            return ("Vintage 未收录在当前认证指标目录中，无法给出本项目认可的计算口径。"
                    "通用业务概念参考（非认证）：Vintage/批次分析通常按放款月份或放款批次划分 cohort，"
                    "再按 MOB（放款后月份）观察逾期、不良、违约等表现；具体观察指标、分子、分母和统计规则"
                    "因业务定义而异。此说明只是通用参考，不能据此推定为你们项目的认证口径或血缘，"
                    "也不构成认证数值。请确认批次字段、观察表现、金额率或笔数率、分母和统计粒度。")
        if intent == "lineage":
            return ("Vintage 未收录在当前认证指标目录中，因此没有可引用的项目认证血缘；"
                    "不会根据通用概念猜测上游表或字段。建议业务人员发起指标建设/治理工单，"
                    "由业务负责人确认用途、批次/MOB定义、分子分母与统计粒度，并由数据负责人补充来源表字段、"
                    "权限、校验规则和生效版本；审核认证发布后再更新指标目录、知识索引与评测集。"
                    "本次仅给出工单建议，没有实际创建或提交工单。")
    if intent == "lineage":
        return ("该指标未认证，当前没有可引用的项目血缘；不会猜测上游表或字段。"
                "请业务人员发起指标建设/治理工单，明确业务定义、负责人、来源表字段、权限和校验规则，"
                "经审核认证发布后再更新指标目录与知识索引。本次没有实际创建或提交工单。")
    candidate = exploration.match(question) if exploration else None
    query_like = (intent == "query" or any(term in question for term in (
        "多少", "数值", "结果", "查询", "查一下", "试算",
    )) or bool(re.search(r"\d{4}-\d{2}-\d{2}", question)))
    if candidate and query_like:
        if candidate.get("status") == "BLOCKED_SOURCE":
            missing = "、".join(candidate.get("missing_fields", []))
            return (f"{candidate['name']}未认证，当前不能提供数值。探索候选缺少{missing}及经确认的分母/统计口径，"
                    "即使进入隔离探索沙箱也无法安全试算。请业务和数据负责人补齐候选口径与来源字段并完成治理审核；"
                    "本次没有执行 SQL 或创建工单。")
        if candidate.get("status") == "DRAFT":
            formula = candidate.get("formula", "待补充")
            return (f"{candidate['name']}尚未认证，当前认证查询不返回该数值。现有候选草稿口径仅供核对：{formula}。"
                    "如需使用合成演示数据进行隔离试算，请明确提交“探索试算 <日期> <指标名称>”；"
                    "结果会明确标注为非认证（NON_CERTIFIED），不能用于正式报表。")
    return ("该指标未认证，无法提供本项目认可的计算口径或数值。"
            "请补充完整指标名称、业务定义、统计范围及分子分母；确认后由业务/数据负责人通过指标建设/治理工单"
            "完成审核认证，再更新指标目录与知识索引。本次没有实际创建或提交工单。")


def _implicit_draft_query(question: str, exploration: ExplorationService, catalog: Catalog) -> bool:
    """Recognize dated value requests for known draft metrics without magic wording."""
    candidate = exploration.match(question)
    if candidate is None or any(term in question for term in (
        "口径", "定义", "如何计算", "怎么计算", "怎么算", "公式", "血缘", "来源", "上游表",
    )):
        return False
    remainder = question.casefold()
    for alias in (candidate["name"], *candidate["aliases"]):
        remainder = remainder.replace(alias.casefold(), " ")
    for metric in catalog.metrics.values():
        if any(alias.casefold() in remainder for alias in (metric["name"], *metric["aliases"])):
            return False
    if candidate["id"] == "vintage_overdue_rate":
        return bool(re.search(
            r"20\d{2}\s*年?\s*(?:0?[1-9]|1[0-2])\s*[-至到~～]\s*(?:0?[1-9]|1[0-2])\s*月",
            question,
        ) or re.search(r"(?<!\d)(?:20\d{2}-\d{2}|20\d{4})(?!\d)", question))
    has_date = bool(re.search(r"20\d{2}-\d{2}-\d{2}", question))
    asks_value = any(term in question for term in ("是多少", "有多少", "多少", "数值", "查询", "查一下", "给我结果"))
    return has_date and asks_value


def detect_intents(question: str, catalog: Catalog) -> list[str]:
    """Return explicit, supported intents in stable display/execution order."""
    if is_exploration_prompt(question):
        return ["exploration"]
    intents = set()
    if any(term in question for term in ("目录版本", "指标目录版本", "目录更新时间", "指标目录更新时间",
                                        "认证目录是什么版本", "认证目录更新时间")):
        intents.add("catalog")
    table_matches = catalog.schema_table_matches(question)
    metric_id = catalog.match(question)
    explicit_schema = bool(table_matches) or (
        catalog.is_schema_question(question) and not (metric_id and "字段" in question)
    )
    if explicit_schema:
        intents.add("schema")
    elif "字段" in question or any(field in question for field in catalog.fields):
        intents.add("field")

    domain_signal = bool(metric_id or re.search(
        r"逾期|贷款|借据|信贷|审批|申请|放款|还款|授信|指标|MOB|VINTAGE", question,
        re.IGNORECASE,
    ))
    if domain_signal and any(term in question for term in (
        "口径", "定义", "如何计算", "怎么计算", "怎么算", "计算", "分子", "分母",
    )):
        intents.add("definition")
    lineage_text = question
    for field in catalog.fields.values():
        lineage_text = lineage_text.replace(field["name"], "")
    if domain_signal and any(term in lineage_text for term in (
        "血缘", "来源", "来自哪些表", "从哪些表", "上游表", "源数据", "从哪里来",
    )):
        intents.add("lineage")

    has_date = bool(re.search(r"\d{4}-\d{2}-\d{2}", question))
    explicit_query = any(term in question for term in ("查询", "查一下", "查下", "展示", "给我结果"))
    asks_for_value = bool(metric_id and any(term in question for term in ("是多少", "有多少", "多少", "数值")))
    asks_for_trend = bool(metric_id and any(term in question for term in (
        "表现", "走势", "趋势", "看看",
    )))
    calculation_metric = _calculation_metric(question, catalog)
    supplied_calculation = bool(
        any(term in question for term in ("是多少", "有多少", "多少", "数值"))
        and _supplied_ratio(question, calculation_metric)
    )
    if supplied_calculation:
        intents.add("calculation")
    negated_value_request = bool(re.search(
        r"(?:不要|不必|无需|不需要|不用|别)(?:再)?\s*(?:查询|查|返回|提供)?\s*(?:数值|数据|结果)", question,
    ))
    if ((asks_for_value or asks_for_trend) and not supplied_calculation and not negated_value_request
            or explicit_query and (has_date or not intents)
            or has_date and not intents):
        intents.add("query")

    ordered = [intent for intent in ("catalog", "definition", "field", "schema", "lineage", "calculation",
                                     "query", "exploration") if intent in intents]
    return ordered or ["other"]


def parse_question(question: str, previous: TaskState, catalog: Catalog,
                   forced_intent: str | None = None) -> tuple[str, TaskState]:
    metric_in_question = (_calculation_metric(question, catalog) if forced_intent == "calculation"
                          else catalog.match(question))
    is_contextual_followup = (
        metric_in_question is None
        and bool(previous.metric_id)
        and bool(_FOLLOWUP_PATTERN.fullmatch(question))
    )
    # Only a clear elliptical follow-up may inherit session context. A newly
    # named metric starts a fresh task, including its date and data-status scope.
    task = previous.model_copy(deep=True) if is_contextual_followup else TaskState()
    metric_id = metric_in_question
    # The certified delinquency ratio is specifically an amount ratio. Do not
    # let substring matching silently reinterpret explicitly different credit-risk concepts.
    if metric_id == "overdue_rate" and any(term in question.upper()
                                           for term in ("M1+", "M1＋", "不良率", "笔数逾期率", "VINTAGE")):
        metric_id = None
    schema_matches = catalog.schema_table_matches(question)
    schema_question = bool(schema_matches) or catalog.is_schema_question(question)
    if forced_intent:
        intent = forced_intent
    elif any(term in question for term in ("目录版本", "指标目录版本", "目录更新时间", "指标目录更新时间",
                                          "认证目录是什么版本", "认证目录更新时间")):
        intent = "catalog"
    elif schema_question:
        intent = "schema"
    elif "字段" in question or any(field in question for field in catalog.fields):
        intent = "field"
    elif any(term in question for term in ("血缘", "来源", "上游表")):
        intent = "lineage"
    elif any(term in question for term in ("口径", "定义", "如何计算", "怎么计算", "怎么算", "计算")):
        intent = "definition"
    else:
        intent = "query"
    followup = is_contextual_followup
    if metric_id:
        task.metric_id = metric_id
    elif intent not in {"catalog", "field", "schema"} and not followup:
        return "refused", TaskState()
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", question)
    if dates:
        if len(dates) > 2:
            return "clarify", TaskState(metric_id=task.metric_id, pending=["dates"])
        try:
            task.start_date, task.end_date = date.fromisoformat(dates[0]), date.fromisoformat(dates[-1])
        except ValueError:
            return "clarify", TaskState(metric_id=task.metric_id, pending=["dates"])
    if any(word in question for word in _RELATIVE_TIME_TERMS):
        # 不把本机系统日期或旧会话日期冒充已确认业务日期。
        task.start_date = task.end_date = None
    requests_realtime = "实时" in question or "REALTIME" in question.upper()
    requests_final = any(word in question.upper() for word in ("离线", "正式", "FINAL"))
    if requests_realtime and requests_final:
        task.pending = ["data_status"]
        return "clarify", task
    if requests_realtime:
        task.data_status = "REALTIME"
    elif requests_final:
        task.data_status = "FINAL"
    if "按产品" in question or "各产品" in question:
        task.dimensions = ["biz_date", "product"]
    elif any(word in question for word in ("按天", "按日期", "按日")):
        task.dimensions = ["biz_date"]
    elif any(word in question for word in ("合计", "汇总")):
        task.dimensions = []
    task.pending = []
    if not task.metric_id and intent not in {"catalog", "field", "schema"}:
        task.pending.append("metric")
    if intent == "query" and (not task.start_date or not task.end_date):
        task.pending.append("dates")
    if intent == "query" and "渠道" in question:
        # Keep this as a query branch so a compound answer can explain why its
        # numeric part is unavailable without losing the other knowledge parts.
        return intent, task
    return ("clarify" if task.pending else intent), task


class QuestionService:
    def __init__(self, db: Path, memory=None, catalog=None, retrieval=None, result_cache=None,
                 intent_router=None):
        self.db = db
        self.catalog = catalog or Catalog()
        self.retrieval = retrieval or LocalRetrieval(self.catalog)
        self.exploration = ExplorationService(db)
        self.memory = memory or StatelessMemory()
        self.result_cache = result_cache or NullResultCache()
        self.intent_router = intent_router or DeepSeekIntentRouter.from_environment()
        # 本地单 worker 同会话串行；生产 Redis 需加跨实例 CAS/会话锁。
        self.session_locks = [threading.Lock() for _ in range(64)]
        self.workflow = self._build_graph()

    def _build_graph(self):
        def retrieved_citations(documents):
            return [{
        "id": f"chunk:{document['chunk_id']}", "title": document["title"],
        "source": document["source"], "version": document.get("doc_version", document["version"]),
        "updated_at": document["updated_at"], "domain": document["domain"],
            } for document in documents]

        def retrieval_question(state, intent, user):
            question = state["question"]
            task = state["task"]
            if (intent in {"definition", "field", "lineage"} and task.metric_id
                    and not self.catalog.match(question) and _FOLLOWUP_PATTERN.fullmatch(question)):
                # A prior user-validated metric may fill an elliptical follow-up
                # for retrieval. The original request and permission checks stay
                # current-turn scoped; memory never supplies identity or access.
                metric = self.catalog.metric(task.metric_id, user, intent)
                return f"{metric['name']} {question}"
            return question

        def route(state):
            intent, task = parse_question(
                state["question"], state["task"], self.catalog, state.get("forced_intent")
            )
            return {"intent": intent, "task": task, "steps": ["route"]}

        def access(state):
            intent, task = state["intent"], state["task"]
            if intent in {"clarify", "refused"}:
                return {"steps": [*state["steps"], "authorize"]}
            try:
                user = state["resolver"]()
                if intent == "catalog":
                    authorize(user, "catalog", ["analyst", "operator"])
                elif intent == "field":
                    authorize(user, "field", ["analyst", "operator"])
                elif intent == "schema":
                    authorize(user, "schema", ["analyst", "operator"])
                else:
                    self.catalog.metric(task.metric_id, user, intent)
            except AccessDenied:
                return {"intent": "refused", "steps": [*state["steps"], "authorize"]}
            return {"steps": [*state["steps"], "authorize"]}

        def tool(state):
            intent, task = state["intent"], state["task"]
            retrieval = None
            if intent == "clarify":
                message = "请补充一个认证指标及明确日期，例如 2026-03-01 至 2026-03-02。"
                if "data_status" in task.pending:
                    message = "请明确选择 REALTIME（实时状态）或 FINAL（离线认证结果）；本次未执行查询。"
                if task.metric_id and "dates" in task.pending and "上月" in state["question"]:
                    message = ("请明确上月的统计方式和业务日期：月末单日逾期率，"
                               "还是上月每日逾期率序列；本次未返回数值。")
                elif task.metric_id and "dates" in task.pending:
                    message = "请补充明确的业务日期；本次未沿用其他指标问题中的日期，也未执行查询。"
                elif "metric" in task.pending:
                    message = "请补充要查询的认证指标；本次没有可安全沿用的指标上下文。"
                result = Result(status="needs_clarification", intent=intent, metric_id=task.metric_id,
                                message=message)
            elif intent == "refused":
                result = Result(status="refused", intent=intent,
                                message=_unregistered_metric_message(
                                    state["question"], state.get("forced_intent") or intent,
                                    self.exploration,
                                ))
            else:
                try:
                    user = state["resolver"]()  # 每次工具调用重新获取身份与权限。
                    if intent == "catalog":
                        authorize(user, "catalog", ["analyst", "operator"])
                        document = self.catalog.document
                        citation = {
                            "id": "catalog:credit-metrics",
                            "title": "认证指标目录",
                            "source": "credit/data/catalog.json",
                            "version": document["version"],
                            "updated_at": document["updated_at"],
                            "domain": "credit",
                        }
                        result = Result(
                            status="ok", intent=intent,
                            message=(f"当前认证指标目录版本：{document['version']}；"
                                     f"更新时间：{document['updated_at']}；"
                                     f"生效时间：{document.get('effective_from', '未记录')}。"),
                            citations=[citation],
                        )
                    elif intent == "schema":
                        rows = self.catalog.lookup_schema(state["question"], user)
                        retrieval = self.retrieval.search(state["question"], user)
                        parent_ids = {f"schema:{row['table_kind']}:{row['table_id']}" for row in rows}
                        evidence = [document for document in retrieval.documents
                                    if document.get("parent_id") in parent_ids]
                        if not rows or not evidence:
                            raise AccessDenied("未检索到认证表结构")
                        result = Result(
                            status="ok", intent=intent,
                            message=f"已从认证字典中检索到 {len(rows)} 条表结构记录。",
                            rows=rows, citations=retrieved_citations(evidence), warnings=retrieval.warnings,
                        )
                    elif intent == "field":
                        fields = self.catalog.lookup_fields(state["question"], user)
                        retrieval = self.retrieval.search(state["question"], user)
                        field_ids = {f"field:{field['id']}" for field in fields}
                        evidence = [document for document in retrieval.documents if document["id"] in field_ids]
                        result = Result(status="ok" if fields else "refused", intent=intent,
                                        message="认证字段定位结果" if fields else "字段未认证或不可访问",
                                        rows=[{k: v for k, v in f.items() if k not in {"roles", "citation"}}
                                              for f in fields],
                                        citations=[*[f["citation"] for f in fields],
                                                   *retrieved_citations(evidence)],
                                        warnings=retrieval.warnings)
                    else:
                        metric = self.catalog.metric(task.metric_id, user, intent)
                        citation = self.catalog.citation(metric)
                        if intent in {"definition", "lineage"}:
                            retrieval = self.retrieval.search(retrieval_question(state, intent, user), user)
                            evidence = [document for document in retrieval.documents
                                        if document["id"] == metric["id"]]
                            if not evidence:
                                raise AccessDenied("未检索到认证知识")
                        if intent == "definition":
                            result = Result(status="ok", intent=intent, metric_id=metric["id"],
                                            message=metric["definition"],
                                            citations=[citation, *retrieved_citations(evidence)],
                                            warnings=retrieval.warnings)
                        elif intent == "lineage":
                            result = Result(status="ok", intent=intent, metric_id=metric["id"],
                                            message=" → ".join(metric["lineage"]),
                                            citations=[citation, *retrieved_citations(evidence)],
                                            warnings=retrieval.warnings)
                        elif intent == "calculation":
                            operands = _supplied_ratio(state["question"], metric["id"])
                            if operands is None or operands[1] == 0:
                                result = Result(
                                    status="needs_clarification", intent=intent, metric_id=metric["id"],
                                    message="请明确给出与认证口径一致的分子、分母及单位；分母不能为零。本次未查询数仓。",
                                    citations=[citation],
                                )
                            else:
                                numerator, denominator = operands
                                percentage = ((numerator / denominator) * 100).quantize(
                                    Decimal("0.0001"), rounding=ROUND_HALF_UP)
                                displayed = format(percentage.normalize(), "f")
                                result = Result(
                                    status="ok", intent=intent, metric_id=metric["id"],
                                    message=(f"依据题目给定数值演算：{displayed}%。"
                                             f"认证口径：{metric['definition']}"
                                             f"目录版本：{citation['version']}；更新时间：{citation['updated_at']}。"
                                             "这不是数仓查询结果。"),
                                    citations=[citation],
                                )
                        elif "渠道" in state["question"]:
                            result = Result(status="unavailable", intent="query", metric_id=metric["id"],
                                            data_status=task.data_status,
                                            message="数值暂不可用：渠道不是该认证指标的查询维度；"
                                                    "请先确认渠道定义及数据来源，并明确业务日期或月末口径。",
                                            citations=[citation])
                        else:
                            request = MetricQuery(metric_id=task.metric_id, start_date=task.start_date,
                                                  end_date=task.end_date, data_status=task.data_status,
                                                  group_by=task.dimensions)
                            result = self._query(request, user, allow_data_cache=state["allow_data_cache"])
                except (AccessDenied, SQLRejected):
                    result = Result(status="refused", intent=intent,
                                    message="查询超出认证范围、当前权限或指标时间语义约束")
                except ValueError:
                    result = Result(status="needs_clarification", intent=intent,
                                    message="请检查日期顺序、跨度（最多 93 天）与分组维度")
                except sqlite3.Error:
                    result = Result(status="unavailable", intent=intent,
                                    message="数据源暂不可用，本次未返回查询结果")
            if retrieval is not None:
                result.retrieval_backend = retrieval.backend
            return {"result": result, "steps": [*state["steps"], "tool"]}

        def validate(state):
            result = state["result"]
            if result.status == "ok":
                try:
                    user = state["resolver"]()
                    if result.metric_id:
                        self.catalog.metric(result.metric_id, user, result.intent)
                    elif result.intent == "catalog":
                        authorize(user, "catalog", ["analyst", "operator"])
                    elif result.intent == "schema":
                        authorize(user, "schema", ["analyst", "operator"])
                    else:
                        for row in result.rows:
                            field = self.catalog.fields[row["id"]]
                            authorize(user, "field", field["roles"])
                except AccessDenied:
                    result = Result(status="refused", intent=state["intent"], message="权限已变化，请重新发起查询")
            result.trace_id = state["trace_id"]
            result.steps = [*state["steps"], "validate", "cite", "audit"]
            return {"result": result}

        graph = StateGraph(WorkflowState)
        for name, node in (("route", route), ("authorize", access), ("tool", tool), ("validate", validate)):
            graph.add_node(name, node)
        graph.add_edge(START, "route")
        graph.add_edge("route", "authorize")
        graph.add_edge("authorize", "tool")
        graph.add_edge("tool", "validate")
        graph.add_edge("validate", END)
        return graph.compile()

    def _query(self, request, user, *, allow_data_cache=True):
        metric = self.catalog.metric(request.metric_id, user, "query")
        sql = compile_query(request, user, self.catalog)
        table = "ads_credit_daily" if request.data_status == "FINAL" else "ads_credit_realtime"
        # 同请求范围内发布信息作为数据来源引用，不能用文档更新时间冒充数据新鲜度。
        with closing(connect(self.db)) as conn:
            provenance = conn.execute(
                f"SELECT DISTINCT source_batch,published_at,biz_date FROM {table} "
                "WHERE biz_date BETWEEN ? AND ? AND domain = ? "
                "ORDER BY biz_date,source_batch,published_at",
                (str(request.start_date), str(request.end_date), "credit"),
            ).fetchall()
            if request.data_status == "FINAL":
                for published in provenance:
                    batch = conn.execute(
                        "SELECT status FROM ingestion_batch WHERE batch_id=? AND biz_date=?",
                        (published["source_batch"], published["biz_date"]),
                    ).fetchone()
                    checks = conn.execute(
                        "SELECT check_name,status FROM quality_check WHERE batch_id=?",
                        (published["source_batch"],),
                    ).fetchall()
                    passed = {check["check_name"] for check in checks if check["status"] == "PASS"}
                    if (batch is None or batch["status"] != "FINAL"
                            or not _FINAL_QUALITY_CHECKS.issubset(passed)
                            or any(check["status"] != "PASS" for check in checks)):
                        return Result(status="unavailable", intent="query", metric_id=metric["id"],
                                      data_status="FINAL", message="FINAL 发布状态或质量校验未通过，数值暂不可用")
        publication = [{"biz_date": row["biz_date"], "source_batch": row["source_batch"],
                        "published_at": row["published_at"]} for row in provenance]
        final_key = None
        if allow_data_cache and request.data_status == "FINAL" and publication:
            final_key = data_cache_key(user, request.model_dump(mode="json"), self.catalog.document, publication)
            payload = self.result_cache.get(final_key)
            if payload is not None:
                try:
                    cached = Result.model_validate(payload)
                    if (cached.status != "ok" or cached.intent != "query"
                            or cached.data_status != "FINAL"):
                        raise ValueError("ineligible FINAL result cache entry")
                except ValueError:
                    pass
                else:
                    cached.from_cache = True
                    return cached
        rows = execute(self.db, sql, request, user, self.catalog)
        if not provenance:
            return Result(status="no_data", intent="query", metric_id=metric["id"], data_status=request.data_status,
                          message="指定范围没有已发布数据；未使用其他状态的数据替代", sql=sql)
        data_citations = [{"id": f"batch:{r['source_batch']}:{r['biz_date']}", "source": table,
                           "updated_at": r["published_at"], "biz_date": r["biz_date"],
                           "version": r["source_batch"], "data_status": request.data_status,
                           "release_status": "FINAL" if request.data_status == "FINAL" else "REALTIME"}
                          for r in provenance]
        warnings = []
        covered = {r["biz_date"] for r in provenance}
        if len(covered) < (request.end_date - request.start_date).days + 1:
            warnings.append("日期范围存在未发布分区，结果仅覆盖引用中的业务日期")
        if request.data_status == "REALTIME":
            warnings.append("REALTIME 为已接收事件的暂态快照，可能尚不完整，不用于正式报表")
        result = Result(status="ok", intent="query", metric_id=metric["id"], data_status=request.data_status,
                        message=f"{metric['name']}，单位：{metric['unit']}；返回 {len(rows)} 行。",
                        rows=rows, sql=sql, citations=[self.catalog.citation(metric), *data_citations], warnings=warnings)
        if final_key:
            cached = result.model_copy(deep=True)
            cached.trace_id, cached.memory_mode, cached.from_cache = "", "stateless", False
            self.result_cache.set(final_key, cached.model_dump(mode="json"))
        return result

    def _audit(self, user: User, result: Result):
        query_hash = hashlib.sha256(result.sql.encode()).hexdigest() if result.sql else None
        with closing(connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO query_audit VALUES(?,?,?,?,?,?,?,?,?)", (
                result.trace_id, user.user_id, result.intent, result.status, result.metric_id,
                len(result.rows), query_hash, user.policy_version, now(),
            ))

    def _save_trace(self, result: Result, request_id: str | None, duration_ms: float,
                    branch_timings: dict | None = None, router_mode: str | None = None,
                    response_model: str | None = None, system_fingerprint: str | None = None):
        sections = result.sections or [result]
        spans = [{"name": "question", "status": result.status, "duration_ms": duration_ms,
                  "evidence_ids": [c.get("id") for c in result.citations],
                  "retrieval_mode": result.retrieval_backend or "not-used"}]
        spans.extend({"name": section.intent, "status": section.status,
                      "duration_ms": (branch_timings or {}).get(section.intent, duration_ms),
                      "evidence_ids": [c.get("id") for c in section.citations],
                      "retrieval_mode": section.retrieval_backend or "not-used"}
                     for section in sections)
        try:
            traces.save(self.db, result.trace_id, request_id, spans,
                        router_mode or getattr(self.intent_router, "mode", "custom"),
                        (getattr(self.intent_router, "model", None)
                         if router_mode in {None, "deepseek-intent-only", "deterministic-fallback"} else None),
                        result.retrieval_backend or "not-used",
                        response_model=response_model, system_fingerprint=system_fingerprint)
        except sqlite3.Error:
            # Mandatory audit has already succeeded; telemetry is best effort.
            pass

    def authorize_result(self, result: Result, user):
        """Recheck today's permissions before exposing a result fetched asynchronously."""
        sections = result.sections or [result]
        for section in sections:
            if section.status != "ok":
                continue
            intent = section.intent
            if intent == "exploration":
                if not self.exploration.authorize(user) or section.metric_id not in self.exploration.candidates:
                    raise AccessDenied("探索候选已失效或当前身份无权访问")
            elif intent == "schema":
                authorize(user, "schema", ["analyst", "operator"])
            elif intent == "catalog":
                authorize(user, "catalog", ["analyst", "operator"])
            elif intent == "field":
                for row in section.rows:
                    field = self.catalog.fields.get(row.get("id"))
                    if field is None:
                        raise AccessDenied("字段已不存在或权限已变化")
                    authorize(user, "field", field["roles"])
            elif section.metric_id:
                self.catalog.metric(section.metric_id, user, intent)
            elif result.metric_id:
                self.catalog.metric(result.metric_id, user, intent)

    def authorize_checkpoint(self, planned_intents: list[str], metric_id: str | None, user):
        """Do not expose saved task metadata after its current authorization is revoked."""
        for intent in planned_intents:
            if intent == "other":
                # An initial local plan may be revised by the configured LLM.
                # 'other' has no metric tool or sensitive result to authorize.
                continue
            if intent == "exploration":
                if not self.exploration.authorize(user) or (
                    metric_id is not None and metric_id not in self.exploration.candidates
                ):
                    raise AccessDenied("探索候选已失效或当前身份无权访问")
            elif intent == "schema":
                authorize(user, "schema", ["analyst", "operator"])
            elif intent == "catalog":
                authorize(user, "catalog", ["analyst", "operator"])
            elif intent == "field":
                authorize(user, "field", ["analyst", "operator"])
            elif metric_id:
                self.catalog.metric(metric_id, user, intent)

    def _question_for_intent(self, question, intent, task):
        metric_id = self.catalog.match(question)
        if (metric_id is None and intent in {"definition", "lineage", "field"}
                and _supplied_ratio(question, _calculation_metric(question, self.catalog))):
            metric_id = _calculation_metric(question, self.catalog)
        if metric_id == "overdue_rate" and "VINTAGE" in question.upper():
            metric_id = None
        if metric_id is None and _FOLLOWUP_PATTERN.fullmatch(question):
            metric_id = task.metric_id
        metric_name = self.catalog.metrics[metric_id]["name"] if metric_id in self.catalog.metrics else None
        if not metric_name:
            return question
        if intent == "definition":
            return f"{metric_name} 指标口径 如何计算"
        if intent == "lineage":
            return f"{metric_name} 数据血缘 来源"
        if intent == "field":
            return f"{metric_name} 对应字段"
        return question

    def _run_intents(self, question, task, resolver, trace_id, intents, allow_data_cache,
                     progress_callback=None, branch_timings=None):
        def run(intent):
            started = time.perf_counter()
            final = self.workflow.invoke({
                "question": self._question_for_intent(question, intent, task),
                "task": task,
                "resolver": resolver,
                "trace_id": trace_id,
                "allow_data_cache": allow_data_cache,
                "forced_intent": intent,
            })
            return intent, final["result"], final["task"], (time.perf_counter() - started) * 1000

        # A request fans out to at most four bounded workers; API-level concurrency
        # limits still apply. All branches use isolated copies of the session task.
        completed = {}
        with ThreadPoolExecutor(max_workers=min(4, len(intents)), thread_name_prefix="ask-intent") as pool:
            futures = {pool.submit(run, intent): (intent, time.perf_counter()) for intent in intents}
            for future in as_completed(futures):
                intent, submitted = futures[future]
                try:
                    finished = future.result()
                    branch = finished[:3]
                    elapsed = finished[3]
                except AccessDenied:
                    branch = (intent, Result(status="refused", intent=intent,
                                             message="当前身份无权访问该分项", trace_id=trace_id),
                              task.model_copy(deep=True))
                    elapsed = (time.perf_counter() - submitted) * 1000
                except Exception:
                    # A failed branch must not discard other completed evidence.
                    # Exception text may contain sensitive question/data fragments.
                    branch = (intent, Result(status="unavailable", intent=intent,
                                             message="该分项暂不可用，请稍后重试", trace_id=trace_id),
                              task.model_copy(deep=True))
                    elapsed = (time.perf_counter() - submitted) * 1000
                completed[intent] = branch
                if branch_timings is not None:
                    branch_timings[intent] = elapsed
                if progress_callback:
                    try:
                        progress_callback(intent, branch[1].status)
                    except Exception:
                        # Progress reporting must never change answer semantics.
                        pass
        return [completed[intent] for intent in intents]

    def _merge_intents(self, branches, trace_id, memory_mode):
        titles = {
            "definition": "指标口径", "field": "字段定位", "schema": "表结构",
            "lineage": "数据血缘", "calculation": "题目给定数值计算", "query": "指标查询",
        }
        checked = []
        for intent, result, branch_task in branches:
            if result.status == "ok" and result.metric_id:
                metric = self.catalog.metrics.get(result.metric_id)
                expected = self.catalog.citation(metric) if metric else None
                metric_citations = [citation for citation in result.citations
                                    if citation.get("id") == f"metric:{result.metric_id}"]
                metric_evidence = [citation for citation in result.citations
                                   if str(citation.get("id", "")).startswith(
                                       f"chunk:metric:{result.metric_id}:")]
                if (expected is None or len(metric_citations) != 1 or
                        any(citation.get(field) != expected.get(field)
                            for citation in [*metric_citations, *metric_evidence]
                            for field in ("version", "updated_at"))):
                    result = Result(status="unavailable", intent=intent, metric_id=result.metric_id,
                                    data_status=result.data_status, trace_id=trace_id,
                                    message="该分项的认证指标版本或更新时间与当前口径不一致，结果暂不可用")
            checked.append((intent, result, branch_task))
        branches = checked
        results = [result for _, result, _ in branches]
        sections = [ResultSection(
            intent=intent,
            title=titles.get(intent, intent),
            status=result.status,
            message=result.message,
            metric_id=result.metric_id,
            data_status=result.data_status,
            rows=result.rows,
            citations=result.citations,
            warnings=result.warnings,
            sql=result.sql,
            retrieval_backend=result.retrieval_backend,
        ) for intent, result, _ in branches]
        citations_by_id = {}
        for result in results:
            for citation in result.citations:
                citation_key = citation.get("id") or repr(sorted(citation.items()))
                citations_by_id.setdefault(citation_key, citation)
        warnings = list(dict.fromkeys(warning for result in results for warning in result.warnings))
        succeeded = any(result.status == "ok" for result in results)
        if succeeded:
            status = "ok"
            if any(result.status != "ok" for result in results):
                warnings.append("部分意图未能完成，请查看各分项状态")
        elif any(result.status == "unavailable" for result in results):
            status = "unavailable"
        elif any(result.status == "needs_clarification" for result in results):
            status = "needs_clarification"
        elif any(result.status == "no_data" for result in results):
            status = "no_data"
        else:
            status = "refused"
        successful_queries = [result for result in results if result.intent == "query" and result.sql]
        data_statuses = {result.data_status for result in results if result.data_status}
        retrieval_backends = {result.retrieval_backend for result in results if result.retrieval_backend}
        names = [titles.get(intent, intent) for intent, _, _ in branches]
        message = f"已并行处理：{'、'.join(names)}。"
        if status != "ok":
            message += "部分或全部分项需要补充信息、权限不足或暂不可用。"
        return Result(
            status=status,
            intent="multi",
            message=message,
            trace_id=trace_id,
            metric_id=next((result.metric_id for result in results
                            if result.status in {"ok", "needs_clarification", "no_data"}
                            and result.metric_id), None),
            data_status=next(iter(data_statuses)) if len(data_statuses) == 1 else None,
            citations=list(citations_by_id.values()),
            warnings=warnings,
            sql=successful_queries[0].sql if len(successful_queries) == 1 else None,
            memory_mode=memory_mode,
            retrieval_backend=(next(iter(retrieval_backends)) if len(retrieval_backends) == 1
                               else "mixed" if retrieval_backends else None),
            steps=["route", "authorize", "parallel_tools", "merge", "validate", "cite", "audit"],
            sections=sections,
        )

    def ask(self, request: Question, resolver, progress_callback=None, plan_callback=None,
            request_id=None) -> Result:
        trace_started = time.perf_counter()
        branch_timings = {}
        user = resolver()
        if is_exploration_prompt(request.question):
            result = self.exploration.from_question(request.question, user)
            try:
                self._audit(user, result)
            except sqlite3.Error:
                return Result(status="unavailable", intent="exploration", trace_id=result.trace_id,
                              message="必需审计存储不可用，本次探索结果未发布",
                              certification_status="NON_CERTIFIED")
            if progress_callback:
                progress_callback("exploration", result.status)
            self._save_trace(result, request_id, (time.perf_counter() - trace_started) * 1000,
                             router_mode="not-routed")
            return result
        # Only exact, one-shot static knowledge answers are cached. A session
        # may depend on task state, and warehouse results require fresh reads.
        is_schema_question = self.catalog.is_schema_question(request.question)
        key = (cache_key(user, request.question, self.catalog.document)
               if not request.session_id and not is_schema_question else None)
        if key:
            payload = self.result_cache.get(key)
            if payload is not None:
                try:
                    cached = Result.model_validate(payload)
                    if cached.status != "ok" or cached.intent not in {"definition", "lineage", "field"}:
                        raise ValueError("ineligible cached result")
                    if cached.metric_id:
                        self.catalog.metric(cached.metric_id, resolver(), cached.intent)
                except (ValueError, AccessDenied):
                    # Invalid or obsolete entries are a cache miss. Do not
                    # remove a shared cache key here; the writer may be racing.
                    pass
                else:
                    cached.trace_id = uuid4().hex
                    cached.memory_mode, cached.from_cache = "cache", True
                    cached.steps = ["cache_hit", "authorize", "validate", "cite", "audit"]
                    try:
                        self._audit(user, cached)
                    except sqlite3.Error:
                        unavailable = Result(status="unavailable", intent=cached.intent, trace_id=cached.trace_id,
                                             message="必需审计存储不可用，本次缓存结果未发布")
                        if progress_callback:
                            progress_callback(cached.intent, unavailable.status)
                        return unavailable
                    if progress_callback:
                        progress_callback(cached.intent, cached.status)
                    self._save_trace(cached, request_id, (time.perf_counter() - trace_started) * 1000,
                                     router_mode="cache-hit")
                    return cached
        session = session_key(user, request.session_id) if request.session_id else None
        lock = self.session_locks[int(hashlib.sha256((session or uuid4().hex).encode()).hexdigest(), 16) % 64]
        with lock:
            state, warnings, memory_mode = TaskState(), [], self.memory.mode if session else "stateless"
            if session:
                try:
                    state = self.memory.get(session) or TaskState()
                except Exception:
                    memory_mode = "stateless"
                    warnings.append("会话存储不可用，已降级无状态；请在本轮给出完整指标和日期")
            trace_id = uuid4().hex
            response_model, system_fingerprint = None, None
            contextual_intents = _contextual_followup_intents(request.question, state, self.catalog)
            if contextual_intents is not None:
                # A short follow-up either has a deterministic intent or needs
                # clarification. Do not send context-free ellipses to the LLM.
                llm_intents = contextual_intents
                router_mode = "context-rule"
            else:
                if hasattr(self.intent_router, "route_with_metadata"):
                    llm_intents, response_model, system_fingerprint = (
                        self.intent_router.route_with_metadata(request.question))
                else:
                    llm_intents = self.intent_router.route(request.question)
                router_mode = (getattr(self.intent_router, "mode", "custom")
                               if llm_intents is not None else
                               "deterministic-fallback" if getattr(self.intent_router, "enabled", False)
                               else "deterministic")
            intents = llm_intents if llm_intents is not None else detect_intents(request.question, self.catalog)
            implicit_exploration = _implicit_draft_query(request.question, self.exploration, self.catalog)
            if implicit_exploration:
                # A dated value request for a registered draft metric is routed
                # to the isolated sandbox; the user need not know internal opt-in wording.
                intents = ["exploration"]
            if (any(term in request.question for term in ("是多少", "有多少", "多少", "数值"))
                    and _supplied_ratio(request.question,
                                        _calculation_metric(request.question, self.catalog))):
                # A classifier may call this a warehouse query. Labelled operands
                # deterministically select the local calculator instead.
                intents = [intent for intent in intents if intent not in {"query", "other"}]
                if "calculation" not in intents:
                    intents.append("calculation")
            if not intents or "other" in intents:
                intents = ["other"]
            if plan_callback:
                try:
                    plan_callback(intents)
                except Exception:
                    # Persisted plan/progress is helpful UI metadata, not answer semantics.
                    pass
            if contextual_intents == []:
                prior_metric = (self.catalog.metrics.get(state.metric_id, {}).get("name")
                                if state.metric_id else None)
                message = (f"你是在追问“{prior_metric}”吗？请说明想看它的数值、口径、字段还是血缘。"
                           if prior_metric else "请补充指标名称和想查询的内容，例如指标数值、口径、字段或血缘。")
                result = Result(status="needs_clarification", intent="clarify", metric_id=state.metric_id,
                                message=message, trace_id=trace_id,
                                steps=["route", "context_clarification"])
                task = state
            elif intents == ["other"]:
                result = Result(
                    status="refused", intent="other",
                    message="我目前只处理信贷指标口径、字段定位、表结构、数据血缘、题目给定数值计算和认证指标查询；"
                            "请补充与这些范围相关的问题。",
                    trace_id=trace_id, steps=["route", "other_refusal"],
                )
                task = state
            elif "exploration" in intents:
                if len(intents) > 1:
                    result = Result(
                        status="needs_clarification", intent="exploration",
                        certification_status="NON_CERTIFIED",
                        message="非认证探索试算与认证查询/解释需分开提交；本次没有执行任何查询。",
                        trace_id=trace_id, steps=["route", "exploration_isolation_check"],
                    )
                    task = TaskState()
                elif not is_exploration_prompt(request.question) and not implicit_exploration:
                    result = Result(
                        status="needs_clarification", intent="exploration",
                        certification_status="NON_CERTIFIED",
                        message="该数值问题涉及非认证探索。若要在隔离沙箱试算，请明确写“探索试算”或“非认证试算”，并提供日期；本次没有执行查询。",
                        trace_id=trace_id, steps=["route", "exploration_opt_in_required"],
                    )
                    task = TaskState()
                else:
                    result = self.exploration.from_question(request.question, resolver())
                    result.trace_id = trace_id
                    task = TaskState()
            elif len(intents) > 1:
                branches = self._run_intents(request.question, state, resolver, trace_id,
                                             intents, not bool(request.session_id), progress_callback,
                                             branch_timings)
                result = self._merge_intents(branches, trace_id, memory_mode)
                # Persist only context from a validated branch. A failed query
                # must not restore a previous session's metric/date on follow-up.
                valid_intents = {section.intent for section in result.sections
                                 if section.status in {"ok", "needs_clarification", "no_data"}}
                valid_branches = [branch for branch in branches if branch[0] in valid_intents]
                task = next((branch_task for intent, _, branch_task in valid_branches if intent == "query"),
                            valid_branches[0][2] if valid_branches else TaskState())
                if progress_callback:
                    for section in result.sections:
                        try:
                            progress_callback(section.intent, section.status)
                        except Exception:
                            pass
            else:
                branch_started = time.perf_counter()
                final = self.workflow.invoke({"question": request.question, "task": state,
                                              "resolver": resolver, "trace_id": trace_id,
                                              "allow_data_cache": not bool(request.session_id),
                                              "forced_intent": intents[0]})
                result, task = final["result"], final["task"]
                branch_timings[intents[0]] = (time.perf_counter() - branch_started) * 1000
            result.memory_mode = memory_mode
            result.warnings.extend(warnings)
            # Mandatory audit: errors never permit returning data without an audit trail.
            try:
                self._audit(user, result)
            except sqlite3.Error:
                unavailable = Result(status="unavailable", intent=result.intent, trace_id=result.trace_id,
                                     message="必需审计存储不可用，本次结果未发布")
                if progress_callback:
                    for intent in intents:
                        progress_callback(intent, unavailable.status)
                return unavailable
            self._save_trace(result, request_id, (time.perf_counter() - trace_started) * 1000,
                             branch_timings, router_mode, response_model, system_fingerprint)
            if session and memory_mode != "stateless" and result.status == "refused":
                try:
                    self.memory.set(session, TaskState())
                except Exception:
                    result.memory_mode = "stateless"
                    result.warnings.append("拒答后未能清除旧会话上下文；请重新开始会话")
            elif session and memory_mode != "stateless" and result.status in {"ok", "needs_clarification", "no_data"}:
                task.turns += 1
                task.trace_ids = [*state.trace_ids, result.trace_id][-5:]
                task.citation_ids = [c["id"] for c in result.citations][:10]
                task.recent_tasks = [*state.recent_tasks, f"{result.intent}:{task.metric_id}:{task.data_status}"][-3:]
                try:
                    self.memory.set(session, task)
                except Exception:
                    result.memory_mode = "stateless"
                    result.warnings.append("本轮会话状态未保存，下轮请提供完整上下文")
            if (key and result.status == "ok" and result.intent in {"definition", "lineage", "field"}
                    and not result.warnings):
                cached = result.model_copy(deep=True)
                cached.trace_id, cached.memory_mode, cached.from_cache = "", "stateless", False
                self.result_cache.set(key, cached.model_dump(mode="json"))
            if len(intents) == 1 and progress_callback:
                progress_callback(intents[0], result.status)
            return result

    def explore(self, request: ExplorationQuery, resolver) -> Result:
        trace_started = time.perf_counter()
        user = resolver()
        result = self.exploration.run(request, user)
        try:
            self._audit(user, result)
        except sqlite3.Error:
            return Result(status="unavailable", intent="exploration", trace_id=result.trace_id,
                          message="必需审计存储不可用，本次探索结果未发布",
                          certification_status="NON_CERTIFIED")
        self._save_trace(result, None, (time.perf_counter() - trace_started) * 1000,
                         router_mode="structured")
        return result

    def query(self, request: MetricQuery, resolver) -> Result:
        """结构化入口同样通过图、授权、审计；不提供可执行 raw SQL 入参。"""
        trace_started = time.perf_counter()
        question = f"{request.metric_id} {request.start_date} 至 {request.end_date} {request.data_status}"
        if request.group_by == ["biz_date", "product"]:
            question += " 按产品"
        # 结构化查询需保留精确的维度与上限，不经自然语言重写。
        user = resolver()
        trace_id = uuid4().hex
        try:
            result = self._query(request, user)
            self.catalog.metric(request.metric_id, resolver(), "query")
        except (AccessDenied, SQLRejected):
            result = Result(status="refused", intent="query", message="当前查询未获授权或不满足认证模板")
        except sqlite3.Error:
            result = Result(status="unavailable", intent="query", message="数据源暂不可用")
        result.trace_id = trace_id
        result.steps = ["authorize", "tool", "validate", "cite", "audit"]
        try:
            self._audit(user, result)
        except sqlite3.Error:
            return Result(status="unavailable", intent="query", trace_id=trace_id,
                          message="必需审计存储不可用，本次结果未发布")
        self._save_trace(result, None, (time.perf_counter() - trace_started) * 1000,
                         router_mode="structured")
        return result
