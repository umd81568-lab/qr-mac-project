import io
import json
import socket
import sqlite3
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app, find_free_port, init_db, print_demo_admin_banner


def make_pdf_bytes() -> bytes:
    return b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"


def make_app(tmp_path, extra_config=None):
    config = {
        "TESTING": True,
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
    assert "mozilla.github.io/pdf.js" in response.get_data(as_text=True)

    with sqlite3.connect(app.config["DATABASE"]) as con:
        con.row_factory = sqlite3.Row
        log = con.execute("SELECT query_params FROM link_access_log LIMIT 1").fetchone()
    assert log is not None
    params = json.loads(log["query_params"])
    assert params["campaign"] == ["fall"]


def test_host_fallback_resolution_when_unregistered_and_unambiguous(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()

    client.post("/domains", data={"hostname": "one.example"})
    domain_one = get_domain_id(app, "one.example")
    assert create_link(client, domain_one, "docs/invoice").status_code == 302

    fallback_response = client.get("/docs/invoice", headers={"Host": "127.0.0.1:5000"})
    assert fallback_response.status_code == 200

    client.post("/domains", data={"hostname": "two.example"})
    domain_two = get_domain_id(app, "two.example")
    assert create_link(client, domain_two, "docs/invoice", filename="other.pdf").status_code == 302

    ambiguous_response = client.get("/docs/invoice", headers={"Host": "192.168.0.10"})
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

    response = client.get("/qr/1.png?size=256")
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("image/png")
    assert "attachment" in response.headers["Content-Disposition"]

    image = Image.open(io.BytesIO(response.data))
    assert image.size == (256, 256)


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


def test_admin_credentials_default_and_env_override(tmp_path, monkeypatch):
    monkeypatch.delenv("ADMIN_USER", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    app = make_app(tmp_path)
    assert app.config["ADMIN_USER"] == "admin"
    assert app.config["ADMIN_PASSWORD"] == "admin123"

    monkeypatch.setenv("ADMIN_USER", "owner")
    monkeypatch.setenv("ADMIN_PASSWORD", "strong-pass")
    app_with_env = make_app(tmp_path / "env")
    assert app_with_env.config["ADMIN_USER"] == "owner"
    assert app_with_env.config["ADMIN_PASSWORD"] == "strong-pass"


def test_demo_admin_banner_prints_only_for_defaults(capsys):
    print_demo_admin_banner("http://127.0.0.1:8000", {"ADMIN_USER": "admin", "ADMIN_PASSWORD": "admin123"})
    output = capsys.readouterr().out
    assert "ADMIN LOGIN (demo defaults — change these!)" in output
    assert "USER:     admin" in output
    assert "PASSWORD: admin123" in output

    print_demo_admin_banner("http://127.0.0.1:8000", {"ADMIN_USER": "owner", "ADMIN_PASSWORD": "strong-pass"})
    output = capsys.readouterr().out
    assert output == ""
