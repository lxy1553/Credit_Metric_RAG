from contextlib import closing

from fastapi.testclient import TestClient

from credit.agent import QuestionService
from credit.api import create_app
from credit.memory import MemoryStore
from credit.models import MetricQuery
from credit.response_cache import MemoryResultCache, cache_key
from credit.security import DEMO_USERS
from credit.warehouse import connect


def headers(token="demo-analyst"):
    return {"Authorization": f"Bearer {token}"}


def test_static_one_shot_cache_keeps_authorization_and_audits_each_request(db):
    app = create_app(db, DEMO_USERS, MemoryStore(), result_cache=MemoryResultCache())
    client = TestClient(app)
    body = {"question": "贷款余额的血缘"}

    first = client.post("/v1/questions", headers=headers(), json=body).json()
    second = client.post("/v1/questions", headers=headers(), json=body).json()

    assert first["status"] == second["status"] == "ok"
    assert first["from_cache"] is False
    assert second["from_cache"] is True and second["memory_mode"] == "cache"
    assert first["trace_id"] != second["trace_id"]
    with closing(connect(db)) as conn:
        audited = conn.execute("SELECT COUNT(*) FROM query_audit").fetchone()[0]
    assert audited == 2


def test_cache_is_not_shared_across_identity_scope_or_sessions(db):
    app = create_app(db, DEMO_USERS, MemoryStore(), result_cache=MemoryResultCache())
    client = TestClient(app)
    body = {"question": "申请笔数定义"}
    assert client.post("/v1/questions", headers=headers(), json=body).json()["from_cache"] is False
    # Visitor can access this public metric, but must not consume the analyst's cache entry.
    visitor = client.post("/v1/questions", headers=headers("demo-visitor"), json=body).json()
    assert visitor["status"] == "ok" and visitor["from_cache"] is False
    assert client.post("/v1/questions", headers=headers("demo-visitor"), json=body).json()["from_cache"] is True

    session = {"question": "申请笔数定义", "session_id": "no-cache-with-history"}
    assert client.post("/v1/questions", headers=headers(), json=session).json()["from_cache"] is False
    assert client.post("/v1/questions", headers=headers(), json=session).json()["from_cache"] is False


def test_final_numeric_queries_are_cached_only_for_current_publication(db):
    app = create_app(db, DEMO_USERS, MemoryStore(), result_cache=MemoryResultCache())
    client = TestClient(app)
    body = {"question": "2026-03-02 贷款余额"}
    first = client.post("/v1/questions", headers=headers(), json=body).json()
    second = client.post("/v1/questions", headers=headers(), json=body).json()
    assert first["status"] == second["status"] == "ok"
    assert first["intent"] == second["intent"] == "query"
    assert first["from_cache"] is False and second["from_cache"] is True

    # Re-publication changes the watermark and therefore forces a fresh read.
    with closing(connect(db)) as conn, conn:
        conn.execute("UPDATE ads_credit_daily SET published_at=? WHERE biz_date=?",
                     ("2026-09-22T01:00:00+00:00", "2026-03-02"))
    third = client.post("/v1/questions", headers=headers(), json=body).json()
    assert third["from_cache"] is False


def test_final_cache_cannot_publish_rows_after_batch_release_is_revoked(db):
    app = create_app(db, DEMO_USERS, MemoryStore(), result_cache=MemoryResultCache())
    client = TestClient(app)
    body = {"question": "2026-03-02 贷款余额"}
    first = client.post("/v1/questions", headers=headers(), json=body).json()
    assert first["status"] == "ok"

    with closing(connect(db)) as conn, conn:
        conn.execute("UPDATE ingestion_batch SET status='LOADED' WHERE biz_date='2026-03-02'")

    second = client.post("/v1/questions", headers=headers(), json=body).json()
    assert second["status"] == "unavailable"
    assert second["rows"] == []
    assert second["sql"] is None
    assert second["citations"] == []


def test_final_query_requires_all_publication_quality_checks(db):
    app = create_app(db, DEMO_USERS, MemoryStore(), result_cache=MemoryResultCache())
    client = TestClient(app)
    body = {"question": "2026-03-02 贷款余额"}
    assert client.post("/v1/questions", headers=headers(), json=body).json()["status"] == "ok"

    with closing(connect(db)) as conn, conn:
        conn.execute("DELETE FROM quality_check WHERE batch_id=? AND check_name=?",
                     ("synthetic-2026-03-02", "loan_balance"))

    result = client.post("/v1/questions", headers=headers(), json=body).json()
    assert result["status"] == "unavailable"
    assert result["rows"] == [] and result["citations"] == []


def test_realtime_data_is_never_cached(db):
    service = QuestionService(db, MemoryStore(), result_cache=MemoryResultCache())
    request = MetricQuery(metric_id="loan_balance", start_date="2026-03-02", end_date="2026-03-02",
                          data_status="REALTIME")
    first = service.query(request, lambda: DEMO_USERS["demo-analyst"])
    second = service.query(request, lambda: DEMO_USERS["demo-analyst"])
    assert first.status == second.status == "ok"
    assert first.from_cache is second.from_cache is False


def test_session_bound_final_query_is_never_cached(db):
    app = create_app(db, DEMO_USERS, MemoryStore(), result_cache=MemoryResultCache())
    client = TestClient(app)
    body = {"question": "2026-03-02 贷款余额", "session_id": "final-query-session"}
    first = client.post("/v1/questions", headers=headers(), json=body).json()
    second = client.post("/v1/questions", headers=headers(), json=body).json()
    assert first["status"] == second["status"] == "ok"
    assert first["from_cache"] is second["from_cache"] is False


def test_cache_key_binds_identity_policy_catalog_and_exact_question():
    catalog = {"version": "v1", "updated_at": "2026-01-01T00:00:00+00:00", "effective_from": "2026-01-01T00:00:00+00:00"}
    analyst = DEMO_USERS["demo-analyst"]
    assert cache_key(analyst, "贷款余额定义", catalog) != cache_key(analyst, "贷款余额口径", catalog)
    assert cache_key(analyst, "贷款余额定义", catalog) != cache_key(DEMO_USERS["demo-visitor"], "贷款余额定义", catalog)
    changed = {**catalog, "version": "v2"}
    assert cache_key(analyst, "贷款余额定义", catalog) != cache_key(analyst, "贷款余额定义", changed)
