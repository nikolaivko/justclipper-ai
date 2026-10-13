"""Keep test files inside the Stories checkout and away from saved voices."""
from pathlib import Path
import uuid

TEST_ROOT = Path(__file__).resolve().parents[1] / 'data/temp' / ('tests-' + uuid.uuid4().hex)


def pytest_configure(config):
    if config.option.basetemp is None:
        config.option.basetemp = str(TEST_ROOT)
