import io
import os
import re
import tempfile

_tmp = tempfile.mkdtemp(prefix="flottebox-test-")
os.environ["DATA_DIR"] = _tmp
os.environ["ENABLE_SCHEDULER"] = "false"
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["SMS_PROVIDER"] = "console"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


class Client(TestClient):
    def form(self, get_url: str, post_url: str | None = None, data: dict | None = None, files=None, **kw):
        token = csrf_of(self.get(get_url).text)
        return self.post(post_url or get_url, data={"csrf_token": token, **(data or {})}, files=files, **kw)


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Client(app) as c:
        yield c


@pytest.fixture()
def owner(client):
    r = client.form("/signup", data={"company": "Utilitaires Express", "full_name": "Jean Gérant",
                                     "email": "owner@test.fr", "password": "motdepasse123"})
    assert r.status_code == 200
    return client


def jpeg_bytes(color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (800, 600), color).save(buf, "JPEG")
    return buf.getvalue()
