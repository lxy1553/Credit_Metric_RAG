import json

import pytest

from credit.demo import sample_bundle, write_bundle
from credit.spark_batch import validate_sources


def test_spark_input_requires_completed_and_verified_batches(tmp_path):
    sources = tmp_path / "sources"
    write_bundle(sources / "2026-03-01", sample_bundle("2026-03-01"))
    assert validate_sources(sources) == [sources / "2026-03-01"]
    marker = json.loads((sources / "2026-03-01" / "_SUCCESS.json").read_text())
    marker["sha256"] = "0" * 64
    (sources / "2026-03-01" / "_SUCCESS.json").write_text(json.dumps(marker))
    with pytest.raises(ValueError, match="SHA256"):
        validate_sources(sources)


def test_spark_input_rejects_empty_source_directory(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    with pytest.raises(ValueError, match="没有找到"):
        validate_sources(sources)
