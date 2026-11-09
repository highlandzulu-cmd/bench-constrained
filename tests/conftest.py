import pytest

from bench.config import load_profiles, load_settings
from bench.tasks import load_tasks


@pytest.fixture(scope="session")
def profiles():
    return load_profiles()


@pytest.fixture(scope="session")
def settings():
    import dataclasses

    # explicit test values: the tests must not depend on the (uncommitted) local settings file
    return dataclasses.replace(load_settings(), allowed_nodes=["test-node-1", "test-node-2"], priority_class="test-low", namespace="bench-constrained")


@pytest.fixture(scope="session")
def tasks():
    return load_tasks()
