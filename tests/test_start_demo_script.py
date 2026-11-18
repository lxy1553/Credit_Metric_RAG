from pathlib import Path


def test_demo_startup_invokes_uvicorn_as_a_python_module():
    script = Path(__file__).parents[1].joinpath("scripts/start_demo.sh").read_text()

    assert "exec .venv/bin/python -m uvicorn credit.api:from_environment" in script
    assert "exec .venv/bin/uvicorn" not in script


def test_demo_startup_passes_single_process_worker_configuration():
    script = Path(__file__).parents[1].joinpath("scripts/start_demo.sh").read_text()

    assert 'CREDIT_API_WORKERS="${CREDIT_API_WORKERS:-1}"' in script
    assert '--workers "${CREDIT_API_WORKERS}"' in script
