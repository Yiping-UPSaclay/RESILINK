from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("RESILINK_ROOT", str(ROOT))
REAL_DATA = Path(os.environ.get("RESILINK_DATA") or ROOT / "data")  # read-only, for proof-of-concept criteria
_DATA = Path(tempfile.mkdtemp(prefix="resilink-test-data-"))
os.environ["RESILINK_DATA"] = str(_DATA)  # tests never write to ./data
sys.path.insert(0, str(ROOT / "apps" / "streamlit"))  # `lib` package of the Streamlit app
sys.path.insert(0, str(ROOT))  # `evals` package (uv run pytest does not add the rootdir like python -m pytest)

SMALL_EXPERIMENT = {
    "experiment": "test_small",
    "release": "synthetic-2026.11.0",
    "dependencies": {
        "lambda_scale": {"prior": "lognormal", "median": 1.0, "gsd": 1.5},
        "backup_scale": {"prior": "lognormal", "median": 1.0, "gsd": 1.4},
    },
    "monte_carlo": {"outer": 3, "inner": 8, "seed": 7, "workers": 1},
    "outputs": {"keep_traces": 4, "tier": "internal"},
}


@pytest.fixture(scope="session")
def data_dir() -> Path:
    return _DATA


@pytest.fixture(scope="session")
def real_data_root() -> Path:
    """This machine's real data directory (./data, or RESILINK_DATA as set before the tests); never written."""
    return REAL_DATA


@pytest.fixture(scope="session")
def release(data_dir: Path):
    from resilink.synthetic import ensure_synthetic_release

    return ensure_synthetic_release()


@pytest.fixture(scope="session")
def net(release):
    from resilink.network.compiled import compile_network

    return compile_network(release)


@pytest.fixture(scope="session")
def small_cfg():
    from resilink.domain.config import ExperimentConfig

    return ExperimentConfig.model_validate(SMALL_EXPERIMENT)


@pytest.fixture(scope="session")
def small_run(small_cfg, release):
    from resilink.experiments import run_experiment

    return run_experiment(small_cfg)


@pytest.fixture(scope="module")
def vcr_config():
    """Recorded cassettes keep no credentials or session cookies (pytest-recording)."""

    def scrub(response):
        response["headers"] = {k: v for k, v in response["headers"].items() if k.lower() != "set-cookie"}
        return response

    return {"filter_headers": ["authorization", "cookie"], "before_record_response": scrub}
