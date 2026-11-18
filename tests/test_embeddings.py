import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest

from credit import embeddings


@pytest.fixture
def fake_embedding(monkeypatch, tmp_path):
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "model.onnx").write_bytes(b"test model weights")
    (model_dir / "tokenizer.json").write_text('{"version": 1}')

    class FakeTextEmbedding:
        constructions = []
        output = None
        calls = []

        @staticmethod
        def get_embedding_size(model_name):
            return 3

        def __init__(self, **kwargs):
            self.constructions.append(kwargs)
            self.model = SimpleNamespace(_model_dir=model_dir)

        def passage_embed(self, texts, **kwargs):
            self.calls.append(("passage", texts, kwargs))
            return iter(self.output if self.output is not None else [[1, 2, 3] for _ in texts])

        def query_embed(self, text, **kwargs):
            self.calls.append(("query", text, kwargs))
            return iter(self.output if self.output is not None else [[1, 2, 3]])

    monkeypatch.setitem(sys.modules, "fastembed", SimpleNamespace(TextEmbedding=FakeTextEmbedding))
    monkeypatch.setattr(embeddings, "version", lambda package: "test-1.0")
    embeddings._file_sha256.cache_clear()
    return FakeTextEmbedding, model_dir


def test_concurrent_queries_initialize_once_offline_on_bounded_cpu(fake_embedding):
    fake, _ = fake_embedding
    embedder = embeddings.FastEmbedder()
    barrier = Barrier(8)

    def query(_):
        barrier.wait()
        return embedder.embed_query("贷款余额")

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(query, range(8))) == [[1.0, 2.0, 3.0]] * 8
    assert len(fake.constructions) == 1
    assert fake.constructions[0]["local_files_only"] is True
    assert fake.constructions[0]["providers"] == ["CPUExecutionProvider"]
    assert fake.constructions[0]["threads"] == 2


def test_only_explicit_preparation_can_download(fake_embedding):
    fake, _ = fake_embedding
    embedder = embeddings.FastEmbedder()
    identity = embedder.prepare(download=True)
    assert fake.constructions[0]["local_files_only"] is False
    embedder.embed_documents(["第一条", "第二条"])
    embedder.embed_query("第一条是什么")
    assert len(fake.constructions) == 1
    assert identity["fingerprint"] == embedder.fingerprint
    assert fake.calls[0][0] == "passage"
    assert fake.calls[1][0] == "query"
    assert fake.calls[0][2] == {"batch_size": 32, "parallel": None}


def test_missing_offline_cache_never_retries_online(fake_embedding, monkeypatch):
    fake, _ = fake_embedding
    flags = []

    def fail(**kwargs):
        flags.append(kwargs["local_files_only"])
        raise OSError("no cache")

    embedder = embeddings.FastEmbedder()
    monkeypatch.setattr(embedder, "_type", fail)
    with pytest.raises(RuntimeError, match="查询阶段不下载模型"):
        embedder.embed_query("贷款余额")
    assert flags == [True]
    assert embedder._model is None
    assert fake.constructions == []


@pytest.mark.parametrize("output, reason", [
    ([[1, 2]], "dimensions"),
    ([[1, 2, float("nan")]], "finite numbers"),
    ([[1, 2, float("inf")]], "finite numbers"),
    ([[0, 0, 0]], "nonzero norm"),
    ([[1, True, 2]], "real numbers"),
    ([[1, "2", 3]], "real numbers"),
    ([], "count mismatch"),
    ([[1, 2, 3], [1, 2, 3]], "count mismatch"),
])
def test_invalid_vectors_are_rejected(fake_embedding, output, reason):
    fake, _ = fake_embedding
    fake.output = output
    with pytest.raises(ValueError, match=reason):
        embeddings.FastEmbedder().embed_query("贷款余额")


def test_empty_documents_do_not_initialize_and_blank_text_is_rejected(fake_embedding):
    fake, _ = fake_embedding
    embedder = embeddings.FastEmbedder()
    assert embedder.embed_documents([]) == []
    with pytest.raises(ValueError, match="nonempty"):
        embedder.embed_documents([" "])
    with pytest.raises(ValueError, match="nonempty"):
        embedder.embed_query("")
    assert fake.constructions == []


def test_fingerprint_reuses_hashes_and_tracks_weights_tokenizer_and_runtime(fake_embedding, monkeypatch):
    _, model_dir = fake_embedding
    first = embeddings.FastEmbedder()
    fingerprint = first.fingerprint
    first.identity["artifacts"].clear()
    assert first.identity["artifacts"]
    assert embeddings.FastEmbedder().fingerprint == fingerprint
    assert embeddings._file_sha256.cache_info().misses == 2
    assert embeddings._file_sha256.cache_info().hits == 2

    (model_dir / "tokenizer.json").write_text('{"version": 2}')
    tokenizer_changed = embeddings.FastEmbedder().fingerprint
    assert tokenizer_changed != fingerprint
    (model_dir / "model.onnx").write_bytes(b"different model weights")
    weights_changed = embeddings.FastEmbedder().fingerprint
    assert weights_changed != tokenizer_changed
    monkeypatch.setattr(embeddings, "version", lambda package: "test-2.0")
    assert embeddings.FastEmbedder().fingerprint != weights_changed
    # The loaded model remains bound to the artifacts captured at initialization.
    assert first.fingerprint == fingerprint


@pytest.mark.parametrize("threads", [0, 5, True, 1.5])
def test_unbounded_or_invalid_thread_counts_are_rejected(threads):
    with pytest.raises(ValueError, match="between 1 and 4"):
        embeddings.FastEmbedder(threads=threads)
