"""Create an immutable Elasticsearch index, verify it, then atomically move the read alias."""

import argparse
import hashlib
import json
import os
import re
from pathlib import Path

import httpx

from credit.catalog import Catalog
from credit.embeddings import DEFAULT_MODEL, FastEmbedder
from credit.knowledge import build_documents, is_effective, utc_now

DEFAULT_ALIAS = "credit-knowledge-current"


def safe_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9-]+", "-", value.casefold()).strip("-")
    if not normalized:
        raise ValueError("index name cannot be empty")
    return normalized[:80]


class IndexPublisher:
    def __init__(self, url: str, alias: str, embedder, client=None, *, clock=utc_now):
        self.url, self.alias, self.embedder = url.rstrip("/"), safe_name(alias), embedder
        self.client = client or httpx.Client(timeout=30.0)
        self.clock = clock

    def mapping(self) -> dict:
        text = {"type": "text", "analyzer": "cjk", "search_analyzer": "cjk"}
        return {
            "settings": {"number_of_shards": 1, "number_of_replicas": 0, "refresh_interval": "1s"},
            "mappings": {
                "dynamic": "strict",
                "_meta": {"embedding_fingerprint": self.embedder.fingerprint},
                "properties": {
                    "chunk_id": {"type": "keyword"}, "parent_id": {"type": "keyword"},
                    "id": {"type": "keyword"}, "kind": {"type": "keyword"},
                    "title": text, "aliases": text, "text": text,
                    "source": {"type": "keyword"}, "version": {"type": "keyword"},
                    "doc_version": {"type": "keyword"},
                    "updated_at": {"type": "date"}, "effective_from": {"type": "date"},
                    "effective_until": {"type": "date"},
                    "allowed_roles": {"type": "keyword"}, "domain": {"type": "keyword"},
                    "certified": {"type": "boolean"}, "content_hash": {"type": "keyword"},
                    "embedding_model": {"type": "keyword"},
                    "embedding_fingerprint": {"type": "keyword"},
                    "embedding": {"type": "dense_vector", "dims": self.embedder.dimensions,
                                  "index": True, "similarity": "cosine"},
                },
            },
        }

    def _request(self, method: str, path: str, **kwargs):
        response = self.client.request(method, f"{self.url}/{path.lstrip('/')}", **kwargs)
        response.raise_for_status()
        return response

    def publish(self, catalog: Catalog | None = None) -> dict:
        catalog = catalog or Catalog()
        documents = build_documents(catalog)
        if not documents or any(not is_effective(d, self.clock()) for d in documents):
            raise ValueError("catalog is empty, not yet effective or expired; alias unchanged")
        corpus_hash = hashlib.sha256(
            "".join(document["content_hash"] for document in documents).encode()
        ).hexdigest()[:12]
        model_hash = self.embedder.fingerprint[:12]
        index = safe_name(f"credit-knowledge-{catalog.document['version']}-{model_hash}-{corpus_hash}")
        exists = self.client.head(f"{self.url}/{index}")
        if exists.status_code == 404:
            self._request("PUT", index, json=self.mapping())
            vectors = self.embedder.embed_documents([document["text"] for document in documents])
            lines = []
            for document, vector in zip(documents, vectors, strict=True):
                payload = {**document, "embedding_model": self.embedder.model_name,
                           "embedding_fingerprint": self.embedder.fingerprint, "embedding": vector}
                lines.extend((json.dumps({"create": {"_index": index, "_id": document["chunk_id"]}}),
                              json.dumps(payload, ensure_ascii=False)))
            bulk = self._request(
                "POST", "_bulk?refresh=wait_for", content=("\n".join(lines) + "\n").encode(),
                headers={"Content-Type": "application/x-ndjson"},
            ).json()
            if bulk.get("errors"):
                raise RuntimeError("bulk indexing failed; alias unchanged (candidate retained for diagnosis)")
        else:
            exists.raise_for_status()
        # A same-name index is immutable. Verify it; never overwrite an active
        # snapshot just because its document count happens to match.
        mappings = self._request("GET", f"{index}/_mapping").json()[index]["mappings"]
        if (mappings.get("_meta", {}).get("embedding_fingerprint") != self.embedder.fingerprint
                or mappings["properties"]["embedding"].get("dims") != self.embedder.dimensions):
            raise RuntimeError("index embedding contract mismatch; alias unchanged")
        stored = self._request("POST", f"{index}/_mget", json={
            "ids": [document["chunk_id"] for document in documents],
        }).json()["docs"]
        if len(stored) != len(documents):
            raise RuntimeError("incomplete index; alias unchanged")
        for expected, item in zip(documents, stored, strict=True):
            source = item.get("_source", {})
            if (not item.get("found") or any(source.get(key) != value for key, value in expected.items())
                    or source.get("embedding_model") != self.embedder.model_name
                    or source.get("embedding_fingerprint") != self.embedder.fingerprint):
                raise RuntimeError("indexed content differs from certified snapshot; alias unchanged")
        count = self._request("GET", f"{index}/_count").json()["count"]
        if count != len(documents):
            raise RuntimeError(f"index verification failed: expected {len(documents)}, got {count}")
        # Same predicate used by both serving branches: catches clock/date issues
        # before an otherwise healthy but entirely invisible index becomes live.
        visible = self._request("POST", f"{index}/_count", json={"query": {"bool": {"filter": [
            {"term": {"certified": True}}, {"term": {"version": catalog.document["version"]}},
            {"term": {"embedding_fingerprint": self.embedder.fingerprint}},
            {"range": {"effective_from": {"lte": self.clock().isoformat()}}},
        ]}}}).json()["count"]
        if visible != len(documents) or any(not is_effective(d, self.clock()) for d in documents):
            raise RuntimeError("knowledge not visible under serving filters; alias unchanged")
        alias_response = self.client.get(f"{self.url}/_alias/{self.alias}")
        old_indices = [] if alias_response.status_code == 404 else sorted(alias_response.raise_for_status().json())
        actions = [{"remove": {"index": old, "alias": self.alias}} for old in old_indices if old != index]
        if index not in old_indices:
            actions.append({"add": {"index": index, "alias": self.alias, "is_write_index": False}})
        if actions:
            self._request("POST", "_aliases", json={"actions": actions})
        return {"status": "published", "index": index, "alias": self.alias,
                "documents": count, "catalog_version": catalog.document["version"],
                "embedding_model": self.embedder.model_name, "dimensions": self.embedder.dimensions,
                "embedding_fingerprint": self.embedder.fingerprint,
                "embedding_identity": self.embedder.identity,
                "previous_indices": old_indices}


def main():
    parser = argparse.ArgumentParser(description="发布认证知识到版本化 Elasticsearch 索引")
    parser.add_argument("--url", default=os.getenv("CREDIT_ELASTICSEARCH_URL", "http://127.0.0.1:9200"))
    parser.add_argument("--alias", default=os.getenv("CREDIT_ELASTICSEARCH_INDEX", DEFAULT_ALIAS))
    parser.add_argument("--model", default=os.getenv("CREDIT_EMBEDDING_MODEL", DEFAULT_MODEL))
    parser.add_argument("--cache", type=Path, default=Path("var/models"))
    parser.add_argument("--output", type=Path, help="保存本次索引发布清单")
    args = parser.parse_args()
    embedder = FastEmbedder(args.model, args.cache)
    embedder.prepare(download=True)  # Downloads are an explicit provisioning step, never a request step.
    publisher = IndexPublisher(args.url, args.alias, embedder)
    result = json.dumps(publisher.publish(), ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(result + "\n")
    print(result)


if __name__ == "__main__":
    main()
