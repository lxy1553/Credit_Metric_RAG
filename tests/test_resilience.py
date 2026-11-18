import pytest

from credit.catalog import Catalog
from credit.resilience import CircuitBreaker, CircuitOpen
from credit.retrieval import ElasticsearchRetrieval, FallbackRetrieval, LocalRetrieval, rerank_for_intent, rrf
from credit.security import DEMO_USERS


def test_breaker_opens_and_half_open_probe_recovers():
    moment = [0.0]
    breaker = CircuitBreaker("demo", threshold=2, cooldown=10, clock=lambda: moment[0])

    def fail():
        raise TimeoutError

    for _ in range(2):
        with pytest.raises(TimeoutError):
            breaker.call(fail)
    with pytest.raises(CircuitOpen):
        breaker.call(lambda: "not called")
    moment[0] = 10
    assert breaker.call(lambda: "ok") == "ok"
    assert breaker.call(lambda: "steady") == "steady"


def test_rrf_fuses_ranks_without_mixing_score_scales():
    result = rrf([[{"id": "a"}, {"id": "b"}], [{"id": "b"}, {"id": "c"}]])
    assert result[0]["id"] == "b"


def test_intent_prior_promotes_fields_only_for_field_questions():
    documents = [
        {"id": "field:balance", "kind": "field_definition", "score": 0.02},
        {"id": "balance", "kind": "metric_definition", "score": 0.019},
    ]
    assert rerank_for_intent("余额是多少", documents)[0]["id"] == "balance"
    assert rerank_for_intent("余额在哪个字段", documents)[0]["id"] == "field:balance"


def test_elasticsearch_request_applies_acl_before_dense_scoring():
    retrieval = ElasticsearchRetrieval(
        "http://es", "credit-current", "demo-v1", "test/model", lambda _: [1.0],
    )
    body = retrieval.body("逾期率", DEMO_USERS["demo-analyst"], [1.0])
    filters = body["query"]["script_score"]["query"]["bool"]["filter"]
    assert {"terms": {"allowed_roles": ["analyst"]}} in filters
    assert {"terms": {"domain": ["credit"]}} in filters
    assert {"term": {"certified": True}} in filters
    assert {"term": {"embedding_model": "test/model"}} in filters


def test_both_elasticsearch_paths_failure_falls_back_to_certified_local_catalog():
    class BrokenClient:
        def post(self, *args, **kwargs):
            raise ConnectionError

    primary = ElasticsearchRetrieval(
        "http://es", "credit-current", "demo-v1", "test/model", lambda _: [1.0], BrokenClient(),
    )
    result = FallbackRetrieval(primary, LocalRetrieval(Catalog())).search(
        "申请通过率怎么计算", DEMO_USERS["demo-visitor"],
    )
    assert result.documents[0]["id"] == "approval_rate"
    assert "降级" in result.warnings[-1]
