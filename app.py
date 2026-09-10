from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import qrcode
from flask import Flask, abort, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_domain(raw: str) -> str:
    value = (raw or "").strip().lower()
    if not value:
        return ""
    if "://" not in value:
        value = f"//{value}"
    parsed = urlparse(value)
    return (parsed.netloc or parsed.path).split("/")[0].strip().lower()


def normalize_path(raw: str) -> str:
    return (raw or "").strip().strip("/")


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)

    root = Path(app.root_path)
    app.config.update(
        DATABASE=str(root / "instance" / "links.db"),
        UPLOAD_FOLDER=str(root / "uploads"),
        QR_FOLDER=str(root / "static" / "qrcodes"),
    )
    if test_config:
        app.config.update(test_config)

    os.makedirs(Path(app.config["DATABASE"]).parent, exist_ok=True)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(app.config["QR_FOLDER"], exist_ok=True)

    init_db(app.config["DATABASE"])

    @app.get("/")
    def dashboard():
        con = sqlite3.connect(app.config["DATABASE"])
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            SELECT id, domain, custom_path, pdf_file_name, qr_file_name, access_count, last_accessed, created_at
            FROM links
            ORDER BY created_at DESC
            """
        ).fetchall()
        con.close()
        return render_template("dashboard.html", links=rows)

    @app.post("/links")
    def create_link():
        domain = normalize_domain(request.form.get("domain", ""))
        custom_path = normalize_path(request.form.get("custom_path", ""))
        pdf = request.files.get("pdf_file")
        if not domain or not custom_path or pdf is None:
            abort(400, "domain, custom path and pdf file are required")
        if not pdf.filename or not pdf.filename.lower().endswith(".pdf"):
            abort(400, "only .pdf files are supported")

        safe_name = secure_filename(pdf.filename)
        saved_pdf_name = f"{uuid.uuid4().hex}-{safe_name}"
        saved_pdf_path = Path(app.config["UPLOAD_FOLDER"]) / saved_pdf_name
        pdf.save(saved_pdf_path)

        con = sqlite3.connect(app.config["DATABASE"])
        con.row_factory = sqlite3.Row
        try:
            cur = con.execute(
                """
                INSERT INTO links (domain, custom_path, pdf_file_name, created_at, access_count)
                VALUES (?, ?, ?, ?, 0)
                """,
                (domain, custom_path, saved_pdf_name, utc_now_iso()),
            )
            link_id = cur.lastrowid

            live_url = f"https://{domain}/{custom_path}"
            qr_file_name = f"{link_id}.png"
            qr_path = Path(app.config["QR_FOLDER"]) / qr_file_name
            qrcode.make(live_url).save(qr_path)
            con.execute(
                "UPDATE links SET qr_file_name = ? WHERE id = ?",
                (qr_file_name, link_id),
            )
            con.commit()
        except sqlite3.IntegrityError:
            con.close()
            saved_pdf_path.unlink(missing_ok=True)
            abort(409, "this domain + path already exists")
        finally:
            if con:
                con.close()

        return redirect(url_for("dashboard"))

    @app.get("/<path:custom_path>")
    def serve_pdf(custom_path: str):
        requested_path = normalize_path(custom_path)
        host = request.host.split(":")[0].strip().lower()

        con = sqlite3.connect(app.config["DATABASE"])
        con.row_factory = sqlite3.Row
        row = con.execute(
            """
            SELECT id, pdf_file_name
            FROM links
            WHERE custom_path = ? AND domain = ?
            """,
            (requested_path, host),
        ).fetchone()
        con.close()

        if row is None:
            abort(404)

        con = sqlite3.connect(app.config["DATABASE"])
        con.execute(
            """
            UPDATE links
            SET access_count = access_count + 1, last_accessed = ?
            WHERE id = ?
            """,
            (utc_now_iso(), row["id"]),
        )
        con.commit()
        con.close()

        pdf_path = Path(app.config["UPLOAD_FOLDER"]) / row["pdf_file_name"]
        if not pdf_path.exists():
            abort(404)

        response = send_file(pdf_path, mimetype="application/pdf", as_attachment=False)
        response.headers["Content-Disposition"] = f'inline; filename="{row["pdf_file_name"]}"'
        return response

    return app


def init_db(db_path: str) -> None:
    con = sqlite3.connect(db_path)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS links (
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
    con.commit()
    con.close()


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
