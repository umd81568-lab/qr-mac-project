import io
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app


def make_pdf_bytes() -> bytes:
    return b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"


def test_create_link_saves_qr_and_renders_dashboard(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "links.db"),
            "UPLOAD_FOLDER": str(tmp_path / "uploads"),
            "QR_FOLDER": str(tmp_path / "qrcodes"),
        }
    )
    client = app.test_client()

    response = client.post(
        "/links",
        data={
            "domain": "mydomain.example",
            "custom_path": "docs/invoice",
            "pdf_file": (io.BytesIO(make_pdf_bytes()), "invoice.pdf"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "https://mydomain.example/docs/invoice" in body

    con = sqlite3.connect(app.config["DATABASE"])
    row = con.execute("SELECT qr_file_name, access_count FROM links LIMIT 1").fetchone()
    con.close()
    assert row is not None
    assert row[0] == "1.png"
    assert row[1] == 0


def test_domain_path_link_serves_pdf_inline_and_tracks_usage(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "links.db"),
            "UPLOAD_FOLDER": str(tmp_path / "uploads"),
            "QR_FOLDER": str(tmp_path / "qrcodes"),
        }
    )
    client = app.test_client()

    create_response = client.post(
        "/links",
        data={
            "domain": "serve.example",
            "custom_path": "files/book",
            "pdf_file": (io.BytesIO(make_pdf_bytes()), "book.pdf"),
        },
        content_type="multipart/form-data",
    )
    assert create_response.status_code == 302

    response = client.get("/files/book", headers={"Host": "serve.example"})
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("application/pdf")
    assert "inline" in response.headers["Content-Disposition"]

    con = sqlite3.connect(app.config["DATABASE"])
    row = con.execute("SELECT access_count, last_accessed FROM links LIMIT 1").fetchone()
    con.close()
    assert row[0] == 1
    assert row[1] is not None
