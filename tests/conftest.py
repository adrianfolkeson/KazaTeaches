"""Shared fixtures.

The spend meter in app/ai/client.py is a module global. Any test that starts the
app (TestClient runs the lifespan) arms it and would leave it pointing at a dead
store for whatever runs next, making failures depend on test order. Disarm it
around every test instead of trusting each one to clean up.
"""

from __future__ import annotations

import pytest

from app.ai import client as ai_client


@pytest.fixture(autouse=True)
def _reset_spend_meter():
    yield
    ai_client.set_meter(None, None)


@pytest.fixture(autouse=True)
def _reset_sitting():
    """The sitting in app/main.py is a module global too, and it carries a
    review count. A test that ends its sitting would otherwise cap whatever ran
    next, and the failure would look like a scheduling bug."""
    from app import main

    main._sitting.update(day=None, asked=set(), attempts={}, reviews=0, started=None)
    yield
    main._sitting.update(day=None, asked=set(), attempts={}, reviews=0, started=None)
