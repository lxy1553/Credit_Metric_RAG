from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


@dataclass(frozen=True)
class User:
    user_id: str
    roles: frozenset[str]
    domains: frozenset[str]
    policy_version: str = "demo-policy-v1"


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=1000)
    session_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,64}$")


class MetricQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric_id: str = Field(min_length=1, max_length=80)
    start_date: date
    end_date: date
    data_status: Literal["FINAL", "REALTIME"] = "FINAL"
    group_by: list[Literal["biz_date", "product"]] = Field(default_factory=lambda: ["biz_date"])
    max_rows: int = Field(default=100, ge=1, le=500)

    @model_validator(mode="after")
    def validate_scope(self):
        if not 0 <= (self.end_date - self.start_date).days <= 92:
            raise ValueError("日期范围必须有序且不超过 93 个自然日")
        if len(self.group_by) > 2 or len(set(self.group_by)) != len(self.group_by):
            raise ValueError("分组维度不能重复")
        return self


class ExplorationQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric_id: str = Field(min_length=1, max_length=80)
    start_date: date
    end_date: date
    group_by: list[Literal["biz_date", "product", "cohort_month", "mob"]] = Field(
        default_factory=lambda: ["biz_date"])
    max_rows: int = Field(default=100, ge=1, le=100)
    accept_non_certified: Literal[True]

    @model_validator(mode="after")
    def validate_scope(self):
        max_days = 366 if self.metric_id == "vintage_overdue_rate" else 30
        if not 0 <= (self.end_date - self.start_date).days <= max_days:
            raise ValueError("探索试算日期范围超出允许范围")
        if len(self.group_by) > 2 or len(set(self.group_by)) != len(self.group_by):
            raise ValueError("分组维度不能重复")
        return self


class ResultSection(BaseModel):
    intent: str
    title: str
    status: Literal["ok", "needs_clarification", "refused", "unavailable", "no_data"]
    message: str
    metric_id: str | None = None
    data_status: str | None = None
    rows: list[dict] = Field(default_factory=list)
    citations: list[dict] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    sql: str | None = None
    retrieval_backend: str | None = Field(default=None, exclude=True)


class Result(BaseModel):
    status: Literal["ok", "needs_clarification", "refused", "unavailable", "no_data"]
    intent: str
    message: str
    trace_id: str = ""
    metric_id: str | None = None
    data_status: str | None = None
    rows: list[dict] = Field(default_factory=list)
    citations: list[dict] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    sql: str | None = None
    memory_mode: str = "stateless"
    from_cache: bool = False
    retrieval_backend: str | None = Field(default=None, exclude=True)
    certification_status: Literal["NON_CERTIFIED"] | None = None
    candidate_version: str | None = None
    candidate_updated_at: str | None = None
    steps: list[str] = Field(default_factory=list)
    sections: list[ResultSection] = Field(default_factory=list)


class QuestionTaskReceipt(BaseModel):
    request_id: str
    status: Literal["queued", "running", "completed", "failed", "interrupted", "cancelled"]
    created_at: str
    expires_at: str


class QuestionTaskView(BaseModel):
    request_id: str
    status: Literal["queued", "running", "completed", "failed", "interrupted", "cancelled", "expired"]
    created_at: str
    expires_at: str
    result: Result | None = None
    planned_intents: list[str] = Field(default_factory=list)
    intent_progress: dict[str, str] = Field(default_factory=dict)
    metric_id: str | None = None
    metric_version: str | None = None
    metric_updated_at: str | None = None
    citation_ids: list[str] = Field(default_factory=list)
    trace_id: str | None = None
    resume_required: bool = False
    message: str | None = None
