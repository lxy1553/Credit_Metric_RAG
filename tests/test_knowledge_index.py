import json

import httpx
import pytest

from credit.catalog import Catalog
from credit.knowledge import build_documents
from credit.search_index import IndexPublisher


class FakeEmbedder:
    model_name = "test/model"
    dimensions = 3
    fingerprint = "f" * 64
    identity = {"fingerprint": fingerprint, "model_name": model_name, "dimensions": dimensions}

    def embed_documents(self, texts):
        return [[1.0, 0.0, 0.0] for _ in texts]


def test_certified_documents_are_stable_and_acl_bearing():
    first, second = build_documents(Catalog()), build_documents(Catalog())
    expected = 35 + sum(1 + len(table.get("fields", [])) for _, table in Catalog().schema_tables)
    assert first == second and len(first) == expected
    assert len({item["chunk_id"] for item in first}) == len(first)
    assert all(item["certified"] and item["content_hash"] and item["allowed_roles"] for item in first)
    overdue = [item for item in first if item["id"] == "overdue_rate"]
    assert overdue and {role for item in overdue for role in item["allowed_roles"]} == {"analyst"}
    assert next(item for item in first if item["parent_id"] == "field:biz_date")["id"] == "field:biz_date"
    schema = [item for item in first if item["kind"].startswith("schema_")]
    assert schema and all(item["allowed_roles"] == ["analyst", "operator"] for item in schema)
    assert all(item["doc_version"] in {"v1.9", "2021-06-10"} for item in schema)
    assert any(item["source"].endswith("#借据表") for item in schema)


def test_publish_verifies_count_before_atomic_alias_switch():
    state = {"bulk_docs": 0, "alias_switched": False, "documents": {}, "mapping": None}

    def handler(request: httpx.Request):
        path = request.url.path
        if request.method == "HEAD":
            return httpx.Response(404)
        if request.method == "PUT":
            mapping = json.loads(request.content)
            assert mapping["mappings"]["dynamic"] == "strict"
            assert mapping["mappings"]["properties"]["embedding"]["dims"] == 3
            state["mapping"] = mapping
            return httpx.Response(200, json={"acknowledged": True})
        if path == "/_bulk":
            lines = request.content.decode().strip().splitlines()
            state["bulk_docs"] = len(lines) // 2
            for action, payload in zip(lines[::2], lines[1::2], strict=True):
                state["documents"][json.loads(action)["create"]["_id"]] = json.loads(payload)
            return httpx.Response(200, json={"errors": False, "items": []})
        if path.endswith("/_mapping"):
            index = path.split("/")[1]
            return httpx.Response(200, json={index: {"mappings": state["mapping"]["mappings"]}})
        if path.endswith("/_mget"):
            return httpx.Response(200, json={"docs": [
                {"found": item in state["documents"], "_source": state["documents"].get(item)}
                for item in json.loads(request.content)["ids"]
            ]})
        if path.endswith("/_count"):
            return httpx.Response(200, json={"count": state["bulk_docs"]})
        if path.startswith("/_alias/"):
            return httpx.Response(404)
        if path == "/_aliases":
            state["alias_switched"] = True
            return httpx.Response(200, json={"acknowledged": True})
        raise AssertionError((request.method, str(request.url)))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = IndexPublisher("http://es", "credit-current", FakeEmbedder(), client).publish()
    expected_count = len(build_documents(Catalog()))
    assert result["documents"] == expected_count
    assert state["bulk_docs"] == expected_count and state["alias_switched"]


def test_publish_rejects_future_catalog_without_creating_or_switching_index():
    called = []

    class NoCalls:
        def head(self, *args, **kwargs):
            called.append("head")
            raise AssertionError("future catalog must be rejected before creating an index")

    future = Catalog()
    future.document["effective_from"] = "2099-01-01T00:00:00+00:00"
    with pytest.raises(ValueError, match="not yet effective"):
        IndexPublisher("http://es", "credit-current", FakeEmbedder(), NoCalls()).publish(future)
    assert called == []
