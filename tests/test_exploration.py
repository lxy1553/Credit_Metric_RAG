import time


def auth(token="demo-analyst"):
    return {"Authorization": f"Bearer {token}"}


def test_explicit_exploration_calculates_draft_rate_and_audits(client):
    response = client.post("/v1/questions", headers=auth(), json={
        "question": "探索试算 2026-03-01 申请拒绝率",
        "session_id": "draft-rate",
    })
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "ok"
    assert body["intent"] == "exploration"
    assert body["certification_status"] == "NON_CERTIFIED"
    assert body["metric_id"] == "application_rejection_rate"
    assert body["rows"] == [{"biz_date": "2026-03-01", "value": 0.5}]
    assert "非认证试算" in body["message"]
    assert "正式报表" in body["message"]
    assert body["candidate_version"] == "draft-v1"
    assert body["citations"] == []
    audit = client.get(f"/v1/audit/{body['trace_id']}", headers=auth()).json()
    assert audit["intent"] == "exploration"
    assert audit["metric_id"] == "application_rejection_rate"


def test_natural_worded_explicit_exploration_runs_in_isolated_sandbox(client):
    body = client.post("/v1/questions", headers=auth(), json={
        "question": "请在隔离探索模式下试算 2026-03-01 申请拒绝率",
    }).json()

    assert body["status"] == "ok"
    assert body["intent"] == "exploration"
    assert body["certification_status"] == "NON_CERTIFIED"
    assert body["rows"] == [{"biz_date": "2026-03-01", "value": 0.5}]


def test_unknown_metric_stays_non_numeric_without_registered_candidate(client):
    body = client.post("/v1/questions", headers=auth(), json={
        "question": "2026-03-01 M7 风险转化率是多少",
    }).json()

    assert body["status"] != "ok"
    assert body["rows"] == []
    assert body["sql"] is None


def test_registered_draft_numeric_query_auto_routes_to_non_certified_exploration(client):
    body = client.post("/v1/questions", headers=auth(), json={
        "question": "2026-03-01 申请拒绝率是多少",
    }).json()

    assert body["status"] == "ok"
    assert body["intent"] == "exploration"
    assert body["certification_status"] == "NON_CERTIFIED"
    assert body["rows"] == [{"biz_date": "2026-03-01", "value": 0.5}]
    assert "非认证" in body["message"]


def test_exploration_opt_in_cannot_execute_a_certified_metric_as_a_draft(client):
    body = client.post("/v1/questions", headers=auth(), json={
        "question": "探索试算 2026-03-02 逾期率",
    }).json()

    assert body["status"] == "refused"
    assert body["metric_id"] is None
    assert body["rows"] == []
    assert body["sql"] is None


def test_exploration_denies_operator_and_visitor(client):
    for token in ("demo-operator", "demo-visitor"):
        body = client.post("/v1/questions", headers=auth(token), json={
            "question": "探索试算 2026-03-01 申请拒绝率",
        }).json()
        assert body["status"] == "refused"
        assert body["rows"] == []
        assert body["sql"] is None


def test_vintage_non_month_end_without_synthetic_snapshot_does_not_fabricate_values(client):
    body = client.post("/v1/questions", headers=auth(), json={
        "question": "探索试算 2026-03-02 Vintage 逾期率",
    }).json()

    assert body["intent"] == "exploration"
    assert body["status"] == "no_data"
    assert body["certification_status"] == "NON_CERTIFIED"
    assert "没有可访问" in body["message"]
    assert body["rows"] == []
    assert body["sql"] is None


def test_vintage_exploration_calculates_calendar_month_cohorts_from_sample_snapshots(client):
    body = client.post("/v1/questions", headers=auth(), json={
        "question": "探索试算 202606 Vintage逾期率",
    }).json()

    assert body["status"] == "ok"
    assert body["intent"] == "exploration"
    assert body["certification_status"] == "NON_CERTIFIED"
    assert body["candidate_version"] == "draft-v1"
    assert body["data_status"] == "EXPLORATORY"
    assert {
        (row["cohort_month"], row["mob"]): row["value"] for row in body["rows"]
    }[("2025-07", 11)] == 0.1
    assert {
        (row["cohort_month"], row["mob"]): row["value"] for row in body["rows"]
    }[("2026-05", 1)] == 0.0
    near_grace_boundary = next(row for row in body["rows"]
                               if row["cohort_month"] == "2026-05" and row["mob"] == 1)
    assert near_grace_boundary["overdue_principal_cents"] == 0
    assert near_grace_boundary["disbursed_amount_cents"] == 1_000_000
    assert "日历月差" in body["message"]
    assert "超过宽限期 3 天" in body["message"]
    assert body["citations"]
    assert "draft-v1" == body["candidate_version"]
    assert body["candidate_updated_at"] == "2026-10-14"
    assert "合成" in body["citations"][-1]["source"]
    assert "sandbox_vintage" in body["sql"]
    assert "DROP" not in body["sql"]

    month_form = client.post("/v1/questions", headers=auth(), json={
        "question": "探索试算 2026-06 Vintage逾期率",
    }).json()
    assert month_form["rows"] == body["rows"]


def test_plain_vintage_overdue_question_never_uses_certified_overdue_rate(client):
    body = client.post("/v1/questions", headers=auth(), json={
        "question": "2026-03-02 Vintage 逾期率是多少",
    }).json()

    assert body["status"] == "no_data"
    assert body["metric_id"] == "vintage_overdue_rate"
    assert body["certification_status"] == "NON_CERTIFIED"
    assert body["rows"] == []
    assert body["sql"] is None
    assert "没有可访问" in body["message"]


def test_registered_draft_query_runs_after_agent_infers_exploration_intent(client):
    class ExplorationRouter:
        def route(self, _question):
            return ["exploration"]

    client.app.state.service.intent_router = ExplorationRouter()
    body = client.post("/v1/questions", headers=auth(), json={
        "question": "2026-03-01 申请拒绝率是多少",
    }).json()

    assert body["status"] == "ok"
    assert body["certification_status"] == "NON_CERTIFIED"
    assert body["rows"] == [{"biz_date": "2026-03-01", "value": 0.5}]


def test_mixed_exploration_and_certified_intents_are_clarified_without_execution(client):
    class MixedRouter:
        def route(self, _question):
            return ["query", "exploration"]

    client.app.state.service.intent_router = MixedRouter()
    body = client.post("/v1/questions", headers=auth(), json={
        "question": "2026-03-01 申请拒绝率是多少，同时查认证逾期率",
    }).json()

    assert body["status"] == "needs_clarification"
    assert body["intent"] == "exploration"
    assert body["rows"] == []
    assert body["sql"] is None
    assert "分开提交" in body["message"]


def test_structured_exploration_requires_acknowledgement_and_bounds(client):
    payload = {"metric_id": "application_rejection_rate", "start_date": "2026-03-01",
               "end_date": "2026-03-02", "group_by": ["biz_date", "product"]}
    assert client.post("/v1/explorations", headers=auth(), json=payload).status_code == 422
    accepted = client.post("/v1/explorations", headers=auth(), json={
        **payload, "accept_non_certified": True,
    })
    body = accepted.json()
    assert accepted.status_code == 200
    assert body["status"] == "ok"
    assert {tuple(row[k] for k in ("biz_date", "product")): row["value"] for row in body["rows"]} == {
        ("2026-03-01", "business"): 1.0,
        ("2026-03-01", "consumer"): 0.0,
        ("2026-03-02", "business"): 0.0,
        ("2026-03-02", "consumer"): 0.0,
    }
    assert client.post("/v1/explorations", headers=auth(), json={
        **payload, "accept_non_certified": True, "group_by": ["domain"],
    }).status_code == 422


def test_exploration_cannot_reach_other_domain_or_inject_sql(client):
    body = client.post("/v1/questions", headers=auth(), json={
        "question": "探索试算 2026-03-01 申请拒绝率; DROP TABLE ads_credit_daily",
    }).json()
    assert body["status"] == "ok"
    assert body["rows"] == [{"biz_date": "2026-03-01", "value": 0.5}]
    assert "DROP" not in (body["sql"] or "")
    assert client.get("/v1/catalog", headers=auth()).status_code == 200
    assert "restricted" not in str(body["rows"])


def test_exploration_does_not_resolve_to_certified_metric_in_same_session(client):
    session = "separate-draft"
    prior = client.post("/v1/questions", headers=auth(), json={
        "question": "申请通过率怎么计算", "session_id": session,
    }).json()
    body = client.post("/v1/questions", headers=auth(), json={
        "question": "探索试算 2026-03-01 申请拒绝率", "session_id": session,
    }).json()
    assert prior["metric_id"] == "approval_rate"
    assert body["metric_id"] == "application_rejection_rate"
    assert body["certification_status"] == "NON_CERTIFIED"
    assert body["rows"] == [{"biz_date": "2026-03-01", "value": 0.5}]


def test_async_exploration_checkpoint_can_resume_after_result_expires(client):
    accepted = client.post("/v1/question-tasks", headers=auth(), json={
        "question": "探索试算 2026-03-01 申请拒绝率",
    })
    assert accepted.status_code == 202
    request_id = accepted.json()["request_id"]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        body = client.get(f"/v1/question-tasks/{request_id}", headers=auth()).json()
        if body["status"] == "completed":
            break
        time.sleep(0.01)
    assert body["status"] == "completed"
    assert body["result"]["certification_status"] == "NON_CERTIFIED"
    assert body["planned_intents"] == ["exploration"]

    client.app.state.question_tasks.results.clear()
    interrupted = client.get(f"/v1/question-tasks/{request_id}", headers=auth()).json()
    assert interrupted["status"] == "interrupted"
    assert interrupted["metric_id"] == "application_rejection_rate"
    assert interrupted["resume_required"] is True


def test_vintage_natural_language_period_runs_non_certified_without_opt_in_phrase(client):
    body = client.post("/v1/questions", headers=auth(), json={
        "question": "2025 年 1-12 月逾期率vintage",
    }).json()

    assert body["status"] == "ok"
    assert body["intent"] == "exploration"
    assert body["certification_status"] == "NON_CERTIFIED"
    assert body["data_status"] == "EXPLORATORY"
    assert len(body["rows"]) == 78
    assert {row["cohort_month"] for row in body["rows"]} == {
        f"2025-{month:02d}" for month in range(1, 13)
    }
    assert "2025-01-31" in body["sql"] and "2025-12-31" in body["sql"]
