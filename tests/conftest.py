"""Shared pytest configuration."""

import pytest


@pytest.fixture
def anyio_backend() -> str:
    # Async tests (marked with @pytest.mark.anyio) run on asyncio only.
    return "asyncio"
