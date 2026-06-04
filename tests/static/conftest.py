from __future__ import annotations

import shutil

import pytest


def pytest_runtest_setup(item: pytest.Item) -> None:
    if shutil.which("node") is None:
        pytest.fail("Node.js 24 is required for Node-backed static dashboard pytest tests; install Node.js or run the CI test job.")
