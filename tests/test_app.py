import io
import json
import os
import socket
import sqlite3
import sys
import warnings
import gc
import ipaddress
from pathlib import Path
import base64

from PIL import Image

os.environ.setdefault("ADMIN_PASSWORD", "import-safe-password")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as app_module
from app import create_app, find_free_port, init_db


def make_pdf_bytes() -> bytes:
    return b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"


def make_app(tmp_path, extra_config=None):
    config = {
        "TESTING": True,
        "DISABLE_ADMIN_AUTH": True,
        "DATABASE": str(tmp_path / "links.db"),
        "UPLOAD_FOLDER": str(tmp_path / "uploads"),
        "QR_FOLDER": str(tmp_path / "qrcodes"),
        "SECRET_KEY": "test-secret",
    }
    if extra_config:
        config.update(extra_config)
    return create_app(config)


def get_domain_id(app, hostname: str) -> int:
    with sqlite3.connect(app.config["DATABASE"]) as con:
        row = con.execute("SELECT id FROM domains WHERE hostname = ?", (hostname,)).fetchone()
    assert row is not None
    return row[0]


def create_link(client, domain_id: int, custom_path: str, filename: str = "file.pdf"):
    return client.post(
        "/links",
        data={
            "domain_id": str(domain_id),
            "custom_path": custom_path,
            "pdf_file": (io.BytesIO(make_pdf_bytes()), filename),
        },
        content_type="multipart/form-data",
    )


def basic_auth_headers(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


class FakeHTTPResponse:
    def __init__(self, payload: bytes):
        self._payload = payload
        self._cursor = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def read(self, size: int = -1):
        if size == -1:
            size = len(self._payload) - self._cursor
        if self._cursor >= len(self._payload):
            return b""
        chunk = self._payload[self._cursor : self._cursor + size]
        self._cursor += len(chunk)
        return chunk


class FakeOpener:
    def __init__(self, payload: bytes):
        self._payload = payload

    def open(self, _request, timeout=0):
        return FakeHTTPResponse(self._payload)


def test_domain_crud_and_delete_block_when_used(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()

    create_domain = client.post("/domains", data={"hostname": "example.com"}, follow_redirects=True)
    assert create_domain.status_code == 200
    assert "Domain added." in create_domain.get_data(as_text=True)

    domain_id = get_domain_id(app, "example.com")
    create_resp = create_link(client, domain_id, "docs/invoice")
    assert create_resp.status_code == 302

    blocked_delete = client.post(f"/domains/{domain_id}/delete", follow_redirects=True)
    assert blocked_delete.status_code == 409
    assert "Cannot delete domain" in blocked_delete.get_data(as_text=True)

    client.post("/domains", data={"hostname": "unused.example"})
    unused_domain_id = get_domain_id(app, "unused.example")
    ok_delete = client.post(f"/domains/{unused_domain_id}/delete")
    assert ok_delete.status_code == 302


def test_parameterized_template_path_resolution_and_access_log(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()

    client.post("/domains", data={"hostname": "promo.example"})
    domain_id = get_domain_id(app, "promo.example")

    create_resp = create_link(client, domain_id, "promo/<code>?ref=abc&utm_source=qr")
    assert create_resp.status_code == 302

    response = client.get(
        "/promo/ZX-99?campaign=fall",
        headers={"Host": "promo.example"},
    )
    assert response.status_code == 200
    assert 'iframe src="/_file/1.pdf"' in response.get_data(as_text=True)

    with sqlite3.connect(app.config["DATABASE"]) as con:
        con.row_factory = sqlite3.Row
        log = con.execute("SELECT query_params FROM link_access_log LIMIT 1").fetchone()
    assert log is not None
    params = json.loads(log["query_params"])
    assert params["campaign"] == ["fall"]


def test_localhost_and_ip_host_fallback_resolution_and_ambiguous_404(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()

    client.post("/domains", data={"hostname": "one.example"})
    domain_one = get_domain_id(app, "one.example")
    assert create_link(client, domain_one, "docs/invoice").status_code == 302

    localhost_response = client.get("/docs/invoice", headers={"Host": "localhost:5000"})
    assert localhost_response.status_code == 200

    ip_host_response = client.get("/docs/invoice", headers={"Host": "203.0.113.10"})
    assert ip_host_response.status_code == 200

    client.post("/domains", data={"hostname": "two.example"})
    domain_two = get_domain_id(app, "two.example")
    assert create_link(client, domain_two, "docs/invoice", filename="other.pdf").status_code == 302

    ambiguous_response = client.get("/docs/invoice", headers={"Host": "localhost"})
    assert ambiguous_response.status_code == 404


def test_viewer_and_raw_file_routes_and_download_flag(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()

    client.post("/domains", data={"hostname": "serve.example"})
    domain_id = get_domain_id(app, "serve.example")
    assert create_link(client, domain_id, "files/book").status_code == 302

    viewer = client.get("/files/book", headers={"Host": "serve.example"})
    assert viewer.status_code == 200
    assert viewer.headers["Content-Type"].startswith("text/html")

    raw = client.get("/_file/1.pdf")
    assert raw.status_code == 200
    assert raw.headers["Content-Type"].startswith("application/pdf")
    assert "inline" in raw.headers["Content-Disposition"]

    download_jump = client.get("/files/book?download=1", headers={"Host": "serve.example"})
    assert download_jump.status_code == 302
    assert "/_file/1.pdf?download=1" in download_jump.headers["Location"]

    downloaded = client.get("/_file/1.pdf?download=1")
    assert "attachment" in downloaded.headers["Content-Disposition"]


def test_qr_download_png_size(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()

    client.post("/domains", data={"hostname": "qr.example"})
    domain_id = get_domain_id(app, "qr.example")
    assert create_link(client, domain_id, "codes/path").status_code == 302

    for size in ("256", "512", "1024"):
        response = client.get(f"/qr/1.png?size={size}")
        assert response.status_code == 200
        assert response.headers["Content-Type"].startswith("image/png")
        assert "attachment" in response.headers["Content-Disposition"]

        image = Image.open(io.BytesIO(response.data))
        assert image.size == (int(size), int(size))
        colors = image.convert("RGB").getcolors(maxcolors=1024)
        assert colors is not None
        assert len(colors) <= 2


def test_server_path_import_allowed_and_rejected(tmp_path):
    allowed_root = tmp_path / "allowed"
    blocked_root = tmp_path / "blocked"
    allowed_root.mkdir()
    blocked_root.mkdir()

    allowed_pdf = allowed_root / "report.pdf"
    allowed_pdf.write_bytes(make_pdf_bytes())

    blocked_pdf = blocked_root / "secret.pdf"
    blocked_pdf.write_bytes(make_pdf_bytes())

    app = make_app(tmp_path, {"ALLOWED_IMPORT_ROOTS": str(allowed_root)})
    client = app.test_client()

    client.post("/domains", data={"hostname": "import.example"})
    domain_id = get_domain_id(app, "import.example")

    allowed_resp = client.post(
        "/links",
        data={
            "domain_id": str(domain_id),
            "custom_path": "imports/ok",
            "server_file_path": str(allowed_pdf),
        },
        follow_redirects=True,
    )
    assert allowed_resp.status_code == 200
    assert "Link created." in allowed_resp.get_data(as_text=True)

    rejected_resp = client.post(
        "/links",
        data={
            "domain_id": str(domain_id),
            "custom_path": "imports/no",
            "server_file_path": str(blocked_pdf),
        },
        follow_redirects=True,
    )
    assert rejected_resp.status_code == 400
    assert "outside allowed roots" in rejected_resp.get_data(as_text=True)


def test_admin_auth_required_but_public_routes_open(tmp_path):
    app = make_app(
        tmp_path,
        {
            "DISABLE_ADMIN_AUTH": False,
            "ADMIN_USER": "admin",
            "ADMIN_PASSWORD": "pass123",
        },
    )
    client = app.test_client()

    unauthorized = client.get("/")
    assert unauthorized.status_code == 401
    authorized = client.get("/", headers=basic_auth_headers("admin", "pass123"))
    assert authorized.status_code == 200

    with sqlite3.connect(app.config["DATABASE"]) as con:
        con.execute(
            """
            INSERT INTO links (domain, custom_path, path_pattern, query_string, pdf_file_name, qr_file_name, created_at, access_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            """,
            ("open.example", "open/path", "open/path", "", "sample.pdf", "1.png", "2026-01-01T00:00:00+00:00"),
        )
    upload_path = Path(app.config["UPLOAD_FOLDER"]) / "sample.pdf"
    upload_path.write_bytes(make_pdf_bytes())

    public_link = client.get("/open/path", headers={"Host": "open.example"})
    assert public_link.status_code == 200
    public_file = client.get("/_file/1.pdf")
    assert public_file.status_code == 200
    health = client.get("/health")
    assert health.status_code == 200
    protected_stats = client.get("/api/links/1/stats")
    assert protected_stats.status_code == 401


def test_413_handler_flashes_and_renders_dashboard(tmp_path):
    app = make_app(tmp_path, {"MAX_CONTENT_LENGTH": 128})
    client = app.test_client()

    client.post("/domains", data={"hostname": "size.example"})
    domain_id = get_domain_id(app, "size.example")
    too_big_pdf = b"%PDF-1.4\n" + b"x" * 1024
    response = client.post(
        "/links",
        data={
            "domain_id": str(domain_id),
            "custom_path": "too-big",
            "pdf_file": (io.BytesIO(too_big_pdf), "big.pdf"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert response.status_code == 413
    assert "Uploaded file exceeds maximum size." in response.get_data(as_text=True)


def test_url_import_accept_and_reject_paths(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    client = app.test_client()
    client.post("/domains", data={"hostname": "url.example"})
    domain_id = get_domain_id(app, "url.example")

    monkeypatch.setattr(app_module, "resolve_hostname_ips", lambda _hostname: {ipaddress.ip_address("93.184.216.34")})
    monkeypatch.setattr(app_module.urlrequest, "build_opener", lambda *_args, **_kwargs: FakeOpener(make_pdf_bytes()))
    ok_response = client.post(
        "/links",
        data={
            "domain_id": str(domain_id),
            "custom_path": "url/ok",
            "pdf_url": "https://files.example/report.pdf",
        },
        follow_redirects=True,
    )
    assert ok_response.status_code == 200
    assert "Link created." in ok_response.get_data(as_text=True)

    reject_response = client.post(
        "/links",
        data={
            "domain_id": str(domain_id),
            "custom_path": "url/reject",
            "pdf_url": "ftp://files.example/report.pdf",
        },
        follow_redirects=True,
    )
    assert reject_response.status_code == 400
    assert "must use http or https" in reject_response.get_data(as_text=True)

    monkeypatch.setattr(app_module, "resolve_hostname_ips", lambda _hostname: {ipaddress.ip_address("127.0.0.1")})
    blocked_response = client.post(
        "/links",
        data={
            "domain_id": str(domain_id),
            "custom_path": "url/blocked",
            "pdf_url": "https://internal.example/doc.pdf",
        },
        follow_redirects=True,
    )
    assert blocked_response.status_code == 400
    assert "disallowed address" in blocked_response.get_data(as_text=True)


def test_db_connections_close_without_resourcewarning(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always", ResourceWarning)
        client.post("/domains", data={"hostname": "close.example"})
        domain_id = get_domain_id(app, "close.example")
        create_link(client, domain_id, "close/path")
        client.get("/close/path", headers={"Host": "close.example"})
        gc.collect()
    assert not any(issubclass(w.category, ResourceWarning) for w in captured)


def test_find_free_port_skips_busy_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        busy_port = sock.getsockname()[1]
        free_port = find_free_port(start_port=busy_port, host="127.0.0.1", max_attempts=20)
        assert free_port != busy_port


def test_migration_from_old_schema(tmp_path):
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            CREATE TABLE links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                domain TEXT NOT NULL,
                custom_path TEXT NOT NULL,
                pdf_file_name TEXT NOT NULL,
                qr_file_name TEXT,
                access_count INTEGER NOT NULL DEFAULT 0,
                last_accessed TEXT,
                created_at TEXT NOT NULL,
                UNIQUE (domain, custom_path)
            )
            """
        )
        con.execute(
            "INSERT INTO links (domain, custom_path, pdf_file_name, created_at, access_count) VALUES (?, ?, ?, ?, 0)",
            ("legacy.example", "legacy/path", "legacy.pdf", "2026-01-01T00:00:00+00:00"),
        )

    init_db(str(db_path))

    with sqlite3.connect(db_path) as con:
        columns = {row[1] for row in con.execute("PRAGMA table_info(links)").fetchall()}
        assert "query_string" in columns
        assert "path_pattern" in columns

        row = con.execute(
            "SELECT domain, custom_path, path_pattern, query_string FROM links WHERE id = 1"
        ).fetchone()
        assert row == ("legacy.example", "legacy/path", "legacy/path", "")

        domain_row = con.execute("SELECT hostname FROM domains WHERE hostname = 'legacy.example'").fetchone()
        assert domain_row is not None


def test_migration_does_not_seed_localhost_domain(tmp_path):
    db_path = tmp_path / "fresh.db"
    init_db(str(db_path))
    with sqlite3.connect(db_path) as con:
        localhost_row = con.execute("SELECT hostname FROM domains WHERE hostname = 'localhost'").fetchone()
    assert localhost_row is None
