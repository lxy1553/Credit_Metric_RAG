"""Bounded CPU embeddings; request-time model loads are strictly offline.

Use ``prepare(download=True)`` in the explicit indexing/preparation command.
The artifact fingerprint binds an index to its model weights, tokenizer and
FastEmbed implementation, including pooling changes between library releases.
"""

import copy
import hashlib
import json
import math
from functools import lru_cache
from importlib.metadata import version
from numbers import Real
from pathlib import Path
from threading import Lock

DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
_MODEL_INIT_LOCK = Lock()
_ADAPTER_VERSION = "fastembed-passage-query-v1"


@lru_cache(maxsize=256)
def _file_sha256(path: str, size: int, mtime_ns: int, ctime_ns: int) -> str:
    # File metadata is part of the cache key, never part of the fingerprint.
    # Two instances sharing unchanged weights need not re-read hundreds of MB.
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class FastEmbedder:
    def __init__(self, model_name: str = DEFAULT_MODEL, cache_dir: Path | None = None, *, threads: int = 2):
        if isinstance(threads, bool) or not isinstance(threads, int) or not 1 <= threads <= 4:
            raise ValueError("embedding threads must be an integer between 1 and 4")
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - exercised by deployment packaging
            raise RuntimeError("安装 RAG 依赖：pip install -e '.[rag]'") from exc
        self._type = TextEmbedding
        self.model_name = model_name
        self.cache_dir = cache_dir
        self.threads = threads
        self._model = None
        self._identity = None
        self.dimensions = TextEmbedding.get_embedding_size(model_name)

    @property
    def model(self):
        self._load(download=False)
        return self._model

    def _load(self, *, download: bool) -> None:
        if self._model is not None:
            return
        # Check again under a process-wide lock: concurrent questions, or two
        # wrappers sharing a cache, must not race through model construction.
        with _MODEL_INIT_LOCK:
            if self._model is not None:
                return
            try:
                model = self._type(
                    model_name=self.model_name,
                    cache_dir=str(self.cache_dir) if self.cache_dir else None,
                    threads=self.threads,
                    providers=["CPUExecutionProvider"],
                    local_files_only=not download,
                )
            except Exception as exc:
                if download:
                    raise
                raise RuntimeError(
                    "本地 embedding 模型未准备或无法加载；请先运行知识入库命令准备模型，查询阶段不下载模型"
                ) from exc
            identity = self._build_identity(model)
            self._identity = identity
            # Publish only a fully initialized model and identity to readers.
            self._model = model

    def _build_identity(self, model) -> dict:
        implementation = model.model
        model_dir = Path(implementation._model_dir)
        artifacts = []
        for artifact in sorted(path for path in model_dir.rglob("*") if path.is_file()):
            stat = artifact.stat()
            artifacts.append({
                "path": artifact.relative_to(model_dir).as_posix(),
                "sha256": _file_sha256(
                    str(artifact.resolve()), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
                ),
            })
        if not artifacts:
            raise RuntimeError("embedding model cache contains no verifiable artifacts")
        identity = {
            "model_name": self.model_name,
            "dimensions": self.dimensions,
            "fastembed_version": version("fastembed"),
            "onnxruntime_version": version("onnxruntime"),
            "tokenizers_version": version("tokenizers"),
            "implementation": f"{type(implementation).__module__}.{type(implementation).__qualname__}",
            "adapter_version": _ADAPTER_VERSION,
            "provider": "CPUExecutionProvider",
            "artifacts": artifacts,
        }
        identity["fingerprint"] = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return identity

    def prepare(self, *, download: bool = False) -> dict:
        """Warm a local model; only explicit callers may permit downloading."""
        self._load(download=download)
        return copy.deepcopy(self._identity)

    @property
    def identity(self) -> dict:
        return self.prepare()

    @property
    def fingerprint(self) -> str:
        self._load(download=False)
        return self._identity["fingerprint"]

    def _validate(self, vectors, count: int) -> list[list[float]]:
        result = []
        for vector in vectors:
            values = vector.tolist() if hasattr(vector, "tolist") else vector
            if not isinstance(values, (list, tuple)) or len(values) != self.dimensions:
                raise ValueError(f"embedding vector must have {self.dimensions} dimensions")
            if any(isinstance(value, bool) or not isinstance(value, Real) for value in values):
                raise ValueError("embedding vector must contain real numbers")
            values = [float(value) for value in values]
            if not all(math.isfinite(value) for value in values):
                raise ValueError("embedding vector must contain finite numbers")
            norm = math.hypot(*values)
            if not math.isfinite(norm) or norm == 0:
                raise ValueError("embedding vector must have a finite nonzero norm")
            result.append(values)
        if len(result) != count:
            raise ValueError(f"embedding count mismatch: expected {count}, got {len(result)}")
        return result

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError("embedding documents must be nonempty strings")
        return self._validate(self.model.passage_embed(texts, batch_size=32, parallel=None), len(texts))

    def embed_query(self, text: str) -> list[float]:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("embedding query must be a nonempty string")
        return self._validate(self.model.query_embed(text, batch_size=1, parallel=None), 1)[0]
