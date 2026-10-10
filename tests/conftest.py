"""Shared test configuration; live provider checks remain opt-in scripts."""

import pytest

# Full-harvest simulations and browser-driven discovery: run nightly and on demand,
# not on every push. Select them with ``-m slow`` and skip them with ``-m "not slow"``.
SLOW_MODULES = {"test_pxweb.py", "test_metadata.py"}


def pytest_collection_modifyitems(items):
    for item in items:
        name = item.path.name
        if "workflow" in name or name in SLOW_MODULES:
            item.add_marker(pytest.mark.slow)
