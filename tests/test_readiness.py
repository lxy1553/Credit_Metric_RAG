import httpx

from credit.retrieval import ElasticsearchRetrieval


def test_liveness_and_local_readiness(client):
    assert client.get("/livez").json() == {"status": "live"}
    assert client.get("/readyz").json() == {"status": "ready"}


def test_readiness_fails_when_configured_primary_retrieval_is_down(db):
    from credit.api import create_app
    from credit.memory import MemoryStore
    from credit.security import DEMO_USERS

    class Down:
        mode = "configured-primary-down"

        def ready(self):
            return False

    app = create_app(db, DEMO_USERS, MemoryStore(), retrieval=Down())
    from fastapi.testclient import TestClient
    assert TestClient(app).get("/readyz").status_code == 503


def test_readiness_fails_when_required_session_memory_is_down(db):
    from credit.api import create_app
    from credit.security import DEMO_USERS

    class DownMemory:
        mode = "redis"

        def ready(self):
            return False

    app = create_app(db, DEMO_USERS, DownMemory())
    from fastapi.testclient import TestClient
    assert TestClient(app).get("/readyz").status_code == 503


def test_optional_response_cache_does_not_block_readiness(db):
    from credit.api import create_app
    from credit.memory import MemoryStore
    from credit.security import DEMO_USERS

    class DownCache:
        mode = "redis-exact"

        def ready(self):
            return False

    app = create_app(db, DEMO_USERS, MemoryStore(), result_cache=DownCache())
    from fastapi.testclient import TestClient
    assert TestClient(app).get("/readyz").status_code == 200


def test_configured_task_queue_blocks_readiness_when_unavailable(db):
    from credit.catalog import Catalog
    from credit.memory import MemoryStore
    from credit.readiness import ReadinessChecker
    from credit.retrieval import LocalRetrieval

    class DownQueue:
        def ready(self):
            return False

    checker = ReadinessChecker(db, LocalRetrieval(Catalog()), MemoryStore(), task_queue=DownQueue())

    assert checker.check() is False


def test_elasticsearch_readiness_is_strict_but_does_not_search_documents():
    def handler(request):
        assert request.method == "GET" and request.url.path == "/credit-current/_count"
        return httpx.Response(200, json={"count": 35})

    retrieval = ElasticsearchRetrieval("http://es", "credit-current", "v1", "model", lambda _: [1.0],
                                       client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert retrieval.ready() is True

    class Broken:
        def get(self, *args, **kwargs):
            raise ConnectionError

    assert ElasticsearchRetrieval("http://es", "credit-current", "v1", "model", lambda _: [1.0],
                                  client=Broken()).ready() is False
