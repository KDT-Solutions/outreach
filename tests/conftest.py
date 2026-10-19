import os
import tempfile

# Settings and the engine are created at import time: configure before importing the app.
_tmp = tempfile.mkdtemp(prefix="outreach-test-")
TOKEN = "t" * 20 + "0123456789abcdefghij"
os.environ.update(
    DATABASE_URL=f"sqlite:///{_tmp}/test.db",
    SECRET_KEY="test-secret",
    DATA_ENCRYPTION_KEY="kX3Ztz6bpbW1u6mJm3m8jZ4c0wq0lQkV2u8jQe0x3nE=",
    ADMIN_PASSWORD="test",
    PUBLIC_BASE_URL="https://testserver",
    OUTREACH_API_TOKEN=TOKEN,
    OUTREACH_API_TENANTS="alpha",
    OUTREACH_API_RATE_LIMIT="1000",
    OUTREACH_TEST_ALLOW_DOMAINS="example.com",
    SMTP2GO_API_KEY="smtp-secret-key",
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api_server import app  # noqa: E402
from app.apiv1 import security  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import Campaign, Tenant  # noqa: E402

AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def db_data():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    security.limiter.reset()
    with SessionLocal() as db:
        alpha = Tenant(slug="alpha", name="Alpha AG", from_name="Alpha", from_email="info@alpha.example", footer="Alpha AG, Bahnhofstrasse 1, 8000 Zürich")
        beta = Tenant(slug="beta", name="Beta GmbH", from_name="Beta", from_email="info@beta.example")
        db.add_all([alpha, beta])
        db.flush()
        ca = Campaign(tenant_id=alpha.id, name="Alpha Herbst", steps=[], sourcing_done=True)
        cb = Campaign(tenant_id=beta.id, name="Beta Geheim", steps=[], sourcing_done=True)
        db.add_all([ca, cb])
        db.commit()
        return {"alpha": alpha.id, "beta": beta.id, "campaign_alpha": ca.id, "campaign_beta": cb.id}


@pytest.fixture(scope="session")
def _client():
    # the MCP session manager runs once per process: one lifespan for all tests
    with TestClient(app, base_url="https://testserver") as c:
        yield c


@pytest.fixture
def client(_client, db_data):
    return _client
