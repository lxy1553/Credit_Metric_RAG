"""认证知识检索：本地词典路径，以及可注入已索引向量的 Elasticsearch 双路适配器。"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import httpx

from credit.catalog import Catalog
from credit.knowledge import build_documents, is_effective, utc_now
from credit.models import User
from credit.resilience import CircuitBreaker, retry_call


@dataclass
class SearchResponse:
    documents: list[dict]
    warnings: list[str] = field(default_factory=list)
    branches: dict[str, str] = field(default_factory=dict)
    backend: str = "unknown"


def rrf(paths: list[list[dict]], limit=5, k=60) -> list[dict]:
    scores, documents = {}, {}
    for path in paths:
        seen = set()
        for document in path:
            key = document.get("parent_id") or document["id"]
            if key in seen:
                continue
            seen.add(key)
            rank = len(seen)
            documents.setdefault(key, document)
            scores[key] = scores.get(key, 0.0) + 1 / (k + rank)
    return [{**documents[key], "score": scores[key]}
            for key in sorted(scores, key=lambda key: (-scores[key], key))[:limit]]


def rerank_for_intent(question: str, documents: list[dict]) -> list[dict]:
    """A deterministic type prior; relevance inside a type remains RRF/BM25+dense driven."""
    folded = question.casefold()
    named_schema_field = any(
        document.get("kind") == "schema_field"
        and any(alias and alias.casefold() in folded for alias in document.get("aliases", []))
        for document in documents
    )
    if named_schema_field:
        preferred = "schema_field"
    elif any(term in question for term in ("表结构", "有哪些字段", "字段列表", "主键", "字段类型",
                                          "建表", "对应东华", "源表", "映射到", "对应表")):
        preferred = "schema_table"
    elif any(document.get("kind", "").startswith("schema_") for document in documents):
        preferred = "schema_field"
    elif "字段" in question:
        preferred = "field_definition"
    elif any(word in question for word in ("血缘", "来源", "来自哪些表")):
        preferred = "metric_lineage"
    elif any(word in question for word in ("口径", "定义", "计算", "怎么算")):
        preferred = "metric_definition"
    else:
        preferred = "metric"

    def priority(document):
        kind = document.get("kind", "")
        matched = kind.startswith(preferred) if preferred == "metric" else kind == preferred
        return (not matched, -document.get("score", 0.0), document["id"])

    return sorted(documents, key=priority)


def fuse(question: str, paths: list[list[dict]]) -> list[dict]:
    # Score parents once per branch, then pick an actually retrieved section of
    # the requested knowledge type. Never label a definition as lineage evidence.
    parents = rrf(paths, limit=100)
    candidates = {}
    for path in paths:
        for document in path:
            key = document.get("parent_id") or document["id"]
            candidates.setdefault(key, []).append(document)
    selected = []
    for parent in parents:
        key = parent.get("parent_id") or parent["id"]
        chosen = rerank_for_intent(question, candidates[key])[0]
        selected.append({**chosen, "score": parent["score"]})
    return rerank_for_intent(question, selected)[:5]


class LocalRetrieval:
    mode = "certified-lexical"

    def __init__(self, catalog: Catalog, clock=utc_now):
        self.catalog, self.clock = catalog, clock

    def ready(self) -> bool:
        return True

    def search(self, question: str, user: User) -> SearchResponse:
        # ACL before scoring; never send an unauthorized document to a reranker or model.
        results = []
        if "credit" not in user.domains:
            return SearchResponse(results, backend=self.mode, branches={"local": "ok"})
        for document in build_documents(self.catalog):
            if document.get("kind", "").startswith("schema_") and not self.catalog.is_schema_question(question):
                continue
            if (not user.roles.intersection(document["allowed_roles"])
                    or not is_effective(document, self.clock())):
                continue
            aliases = [document["id"], document["title"], *document["aliases"]]
            folded = question.casefold()
            score = sum(len(alias) for alias in aliases if alias.casefold() in folded)
            if score:
                results.append({**document, "score": score})
        results.sort(key=lambda document: (-document["score"], document["chunk_id"]))
        return SearchResponse(fuse(question, [results]), backend=self.mode, branches={"local": "ok"})


class ElasticsearchRetrieval:
    """Transport adapter. Caller supplies embeddings and indexes the same version/dimension.

    Both branches have ACL + certification + version filters. Do not set this as
    the API source until a real embedding/index integration has passed evaluation.
    """

    mode = "elasticsearch-hybrid-rrf"

    def __init__(self, url: str, index: str, version: str, embedding_model: str, embed, client=None,
                 *, fingerprint: str | None = None, catalog: Catalog | None = None, clock=utc_now):
        if not index.replace("-", "").replace("_", "").isalnum():
            raise ValueError("invalid index name")
        self.url, self.index, self.version = url.rstrip("/"), index, version
        self.embedding_model, self.embed = embedding_model, embed
        self.embedding_fingerprint, self.clock = fingerprint, clock
        self.catalog = catalog
        self.canonical = {d["chunk_id"]: d for d in build_documents(catalog)} if catalog else None
        self.client = client or httpx.Client(timeout=2.0)
        self.lexical_breaker = CircuitBreaker("elasticsearch-lexical")
        self.dense_breaker = CircuitBreaker("elasticsearch-dense")
        self.embedding_breaker = CircuitBreaker("embedding")

    def ready(self) -> bool:
        """Strict readiness: serving may fall back, deployment must not hide ES loss."""
        try:
            response = self.client.get(f"{self.url}/{self.index}/_count")
            response.raise_for_status()
            return isinstance(response.json().get("count"), int)
        except Exception:
            return False

    def body(self, question: str, user: User, vector=None, *, moment=None) -> dict:
        as_of = (moment or self.clock()).isoformat()
        filters = [
            {"terms": {"allowed_roles": sorted(user.roles)}},
            {"terms": {"domain": sorted(user.domains)}},
            {"term": {"certified": True}}, {"term": {"version": self.version}},
            {"term": {"embedding_model": self.embedding_model}},
            {"range": {"effective_from": {"lte": as_of}}},
            {"bool": {"minimum_should_match": 1, "should": [
                {"bool": {"must_not": [{"exists": {"field": "effective_until"}}]}},
                {"range": {"effective_until": {"gt": as_of}}},
            ]}},
        ]
        if self.embedding_fingerprint:
            filters.append({"term": {"embedding_fingerprint": self.embedding_fingerprint}})
        query = {"bool": {"filter": filters}}
        schema_question = bool(self.catalog and self.catalog.is_schema_question(question))
        if schema_question:
            filters.append({"terms": {"kind": ["schema_table", "schema_field"]}})
            table_matches = self.catalog.schema_table_matches(question)
            parent_ids = {f"schema:{kind}:{table['id']}" for kind, table in table_matches}
            if not parent_ids and self.canonical is not None:
                folded = question.casefold()
                parent_ids = {
                    document["parent_id"] for document in self.canonical.values()
                    if document.get("kind") == "schema_field"
                    and any(alias and alias.casefold() in folded for alias in document.get("aliases", []))
                }
            if parent_ids:
                filters.append({"terms": {"parent_id": sorted(parent_ids)}})
            if self.canonical is not None:
                folded = question.casefold()
                field_chunks = [
                    document["chunk_id"] for document in self.canonical.values()
                    if document.get("kind") == "schema_field"
                    and any(len(alias) >= 2 and alias.casefold() in folded
                            for alias in document.get("aliases", []))
                    and (not parent_ids or document.get("parent_id") in parent_ids)
                ]
                if field_chunks:
                    filters.append({"terms": {"chunk_id": field_chunks}})
        else:
            query["bool"]["must_not"] = [{"terms": {"kind": ["schema_table", "schema_field"]}}]
        if vector is None:
            query["bool"]["must"] = [{"multi_match": {
                "query": question, "fields": ["title^3", "aliases^2", "text"],
            }}]
        else:
            query = {"script_score": {"query": query, "script": {
                "source": "cosineSimilarity(params.vector, 'embedding') + 1.0", "params": {"vector": vector},
            }}}
        return {"size": 10, "query": query, "_source": [
            "chunk_id", "parent_id", "id", "kind", "title", "text", "source", "version",
            "doc_version",
            "aliases", "updated_at", "effective_from", "effective_until", "allowed_roles", "domain",
            "certified", "content_hash", "embedding_model", "embedding_fingerprint",
        ]}

    def _search(self, body, user, moment):
        response = self.client.post(f"{self.url}/{self.index}/_search", json=body)
        response.raise_for_status()
        documents = [hit["_source"] for hit in response.json()["hits"]["hits"]]
        valid = []
        for d in documents:
            if (d.get("certified") is not True or d.get("version") != self.version
                    or d.get("embedding_model") != self.embedding_model
                    or (self.embedding_fingerprint
                        and d.get("embedding_fingerprint") != self.embedding_fingerprint)
                    or d.get("domain") not in user.domains
                    or not user.roles.intersection(d.get("allowed_roles", []))
                    or not is_effective(d, moment)):
                continue
            if self.canonical is not None:
                expected = self.canonical.get(d.get("chunk_id"))
                if not expected or any(d.get(key) != value for key, value in expected.items()):
                    raise ValueError("indexed knowledge differs from certified catalog")
            valid.append(d)
        return valid

    def search(self, question: str, user: User) -> SearchResponse:
        warnings, branches, moment = [], {}, self.clock()

        def lexical():
            return retry_call(self.lexical_breaker, lambda: self._search(
                self.body(question, user, moment=moment), user, moment))

        def dense():
            vector = retry_call(self.embedding_breaker, lambda: self.embed(question))
            return retry_call(self.dense_breaker, lambda: self._search(
                self.body(question, user, vector, moment=moment), user, moment))

        paths = []
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [("lexical", executor.submit(lexical)), ("dense", executor.submit(dense))]
            for name, future in futures:
                try:
                    paths.append(future.result())
                    branches[name] = "ok"
                except Exception:
                    paths.append([])
                    warnings.append(f"{name} retrieval unavailable")
                    branches[name] = "unavailable"
        return SearchResponse(fuse(question, paths), warnings, branches, self.mode)


class FallbackRetrieval:
    mode = "elasticsearch-hybrid-rrf+certified-fallback"

    def __init__(self, primary: ElasticsearchRetrieval, fallback: LocalRetrieval):
        self.primary, self.fallback = primary, fallback

    def ready(self) -> bool:
        return self.primary.ready()

    def search(self, question: str, user: User) -> SearchResponse:
        result = self.primary.search(question, user)
        if result.documents or result.branches != {"lexical": "unavailable", "dense": "unavailable"}:
            return result
        fallback = self.fallback.search(question, user)
        return SearchResponse(fallback.documents, [*result.warnings, "已降级到认证本地词法检索"],
                              {**result.branches, **fallback.branches}, fallback.backend)
