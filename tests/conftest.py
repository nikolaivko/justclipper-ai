"""Never let application startup/cleanup tests see the user's live job store."""
import os
from pathlib import Path
import uuid


# Set before test collection imports app.config and the storage singletons.
# This affects only the pytest process; all files remain inside the checkout.
TEST_DATA_ROOT = Path(__file__).resolve().parents[1] / "data" / "temp" / (
    "test-session-" + uuid.uuid4().hex)
os.environ["DATA_ROOT"] = str(TEST_DATA_ROOT)


def pytest_configure(config):
    if config.option.basetemp is None:
        config.option.basetemp = str(TEST_DATA_ROOT / "pytest")

# Ensure project-local binaries are available to simple subprocess calls in tests.
PROJECT_BIN = str(Path(__file__).resolve().parents[1] / "bin")
os.environ["PATH"] = PROJECT_BIN + os.pathsep + os.environ.get("PATH", "")
