from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import quote, urlparse, urlsplit

import qrcode
from PIL import Image
from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from werkzeug.utils import secure_filename

RESERVED_PATH_PREFIXES = {"links", "domains", "static", "qr", "api", "dashboard", "health", "_file"}
PLACEHOLDER_RE = re.compile(r"<([a-zA-Z_][a-zA-Z0-9_]*)>")


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


def split_path_and_query(raw: str) -> tuple[str, str]:
    value = (raw or "").strip()
    if not value:
        return "", ""
    if not value.startswith("/"):
        value = f"/{value}"
    parsed = urlsplit(value)
    path = normalize_path(parsed.path)
    query = parsed.query.strip("& ")
    return path, query


def validate_path_pattern(path_pattern: str) -> str | None:
    if not path_pattern:
        return "custom path is required"
    if ".." in path_pattern.split("/"):
        return "custom path cannot contain '..'"

    segments = [segment for segment in path_pattern.split("/") if segment]
    if not segments:
        return "custom path is required"
    if segments[0].lower() in RESERVED_PATH_PREFIXES:
        return f"path cannot start with reserved prefix '{segments[0]}'"

    for segment in segments:
        if "<" in segment or ">" in segment:
            if not PLACEHOLDER_RE.fullmatch(segment):
                return "path placeholders must be full segments like <code>"
    return None


def pattern_to_regex(path_pattern: str) -> re.Pattern[str]:
    parts = []
    for segment in normalize_path(path_pattern).split("/"):
        placeholder = PLACEHOLDER_RE.fullmatch(segment)
        if placeholder:
            parts.append(f"(?P<{placeholder.group(1)}>[^/]+)")
        else:
            parts.append(re.escape(segment))
    return re.compile(r"^" + "/".join(parts) + r"$")


def match_path(path_pattern: str, requested_path: str) -> dict[str, str] | None:
    regex = pattern_to_regex(path_pattern)
    matched = regex.fullmatch(normalize_path(requested_path))
    if not matched:
        return None
    return matched.groupdict()


def build_live_url(domain: str, path_pattern: str, query_string: str) -> str:
    base = f"https://{domain}/{normalize_path(path_pattern)}"
    if query_string:
        return f"{base}?{query_string}"
    return base


def parse_allowed_import_roots(raw_value: str) -> list[Path]:
    roots: list[Path] = []
    for raw_root in raw_value.split(":"):
        value = raw_root.strip()
        if value:
            roots.append(Path(value).expanduser().resolve())
    return roots


def is_subpath(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def ensure_pdf_signature(path: Path) -> None:
    with path.open("rb") as file_obj:
        signature = file_obj.read(4)
    if signature != b"%PDF":
        raise ValueError("file is not a valid PDF")


def save_uploaded_file(app: Flask, uploaded_file) -> str:
    max_size = int(app.config["MAX_CONTENT_LENGTH"])
    uploaded_file.stream.seek(0, os.SEEK_END)
    file_size = uploaded_file.stream.tell()
    uploaded_file.stream.seek(0)
    if file_size > max_size:
        raise ValueError("uploaded file exceeds maximum size")

    if uploaded_file.stream.read(4) != b"%PDF":
        uploaded_file.stream.seek(0)
        raise ValueError("uploaded file is not a valid PDF")
    uploaded_file.stream.seek(0)

    safe_name = secure_filename(uploaded_file.filename or "uploaded.pdf")
    if not safe_name.lower().endswith(".pdf"):
        safe_name = f"{safe_name}.pdf"
    saved_pdf_name = f"{uuid.uuid4().hex}-{safe_name}"
    saved_pdf_path = Path(app.config["UPLOAD_FOLDER"]) / saved_pdf_name
    uploaded_file.save(saved_pdf_path)
    ensure_pdf_signature(saved_pdf_path)
    return saved_pdf_name


def copy_pdf_from_server_path(app: Flask, source_path: str) -> str:
    raw_path = (source_path or "").strip()
    if not raw_path:
        raise ValueError("server import path is required")

    candidate = Path(raw_path)
    if not candidate.is_absolute():
        raise ValueError("server import path must be absolute")

    allowed_roots = parse_allowed_import_roots(app.config["ALLOWED_IMPORT_ROOTS"])
    if not allowed_roots:
        raise ValueError("no allowed import roots configured")

    normalized = Path(os.path.normpath(str(candidate)))
    if not any(is_subpath(normalized, root) for root in allowed_roots):
        raise ValueError("server import path is outside allowed roots")

    resolved = normalized.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("server import path must point to a file")

    if not any(is_subpath(resolved, root) for root in allowed_roots):
        raise ValueError("server import path is outside allowed roots")

    max_size = int(app.config["MAX_CONTENT_LENGTH"])
    if resolved.stat().st_size > max_size:
        raise ValueError("server file exceeds maximum size")

    safe_name = secure_filename(resolved.name)
    if not safe_name.lower().endswith(".pdf"):
        safe_name = f"{safe_name}.pdf"
    saved_pdf_name = f"{uuid.uuid4().hex}-{safe_name}"
    saved_pdf_path = Path(app.config["UPLOAD_FOLDER"]) / saved_pdf_name
    shutil.copyfile(resolved, saved_pdf_path)
    ensure_pdf_signature(saved_pdf_path)
    return saved_pdf_name


def save_pdf_from_inputs(app: Flask, require_pdf: bool = True) -> str | None:
    uploaded_file = request.files.get("pdf_file")
    server_path = (request.form.get("server_file_path", "") or "").strip()

    modes = 0
    if uploaded_file and uploaded_file.filename:
        modes += 1
    if server_path:
        modes += 1

    if modes == 0:
        if require_pdf:
            raise ValueError("provide one PDF source: upload or server path")
        return None
    if modes > 1:
        raise ValueError("use only one PDF source at a time")

    if uploaded_file and uploaded_file.filename:
        return save_uploaded_file(app, uploaded_file)
    if server_path:
        return copy_pdf_from_server_path(app, server_path)
    return None


def add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    return response


def find_free_port(start_port: int = 8000, host: str = "127.0.0.1", max_attempts: int = 200) -> int:
    port = int(start_port)
    for candidate in range(port, port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, candidate))
            except OSError:
                continue
            return candidate
    raise RuntimeError("could not find a free port")


def detect_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def migrate(db_path: str) -> None:
    with sqlite3.connect(db_path) as con:
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

        columns = {row[1] for row in con.execute("PRAGMA table_info(links)").fetchall()}
        if "query_string" not in columns:
            con.execute("ALTER TABLE links ADD COLUMN query_string TEXT NOT NULL DEFAULT ''")
        if "path_pattern" not in columns:
            con.execute("ALTER TABLE links ADD COLUMN path_pattern TEXT")
            con.execute("UPDATE links SET path_pattern = custom_path WHERE path_pattern IS NULL")

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS domains (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hostname TEXT NOT NULL UNIQUE,
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS link_access_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                link_id INTEGER NOT NULL,
                accessed_at TEXT NOT NULL,
                ip TEXT,
                user_agent TEXT,
                query_params TEXT,
                referrer TEXT,
                FOREIGN KEY(link_id) REFERENCES links(id) ON DELETE CASCADE
            )
            """
        )

        existing_domains = {
            row[0] for row in con.execute("SELECT hostname FROM domains").fetchall() if row[0]
        }
        link_domains = {
            normalize_domain(row[0])
            for row in con.execute("SELECT DISTINCT domain FROM links").fetchall()
            if row[0]
        }
        now = utc_now_iso()
        for domain in sorted(link_domains):
            if domain and domain not in existing_domains:
                con.execute(
                    "INSERT INTO domains (hostname, is_default, created_at) VALUES (?, 0, ?)",
                    (domain, now),
                )

        domain_count = con.execute("SELECT COUNT(*) FROM domains").fetchone()[0]
        if domain_count == 0:
            con.execute(
                "INSERT INTO domains (hostname, is_default, created_at) VALUES (?, 1, ?)",
                ("localhost", now),
            )
        elif con.execute("SELECT COUNT(*) FROM domains WHERE is_default = 1").fetchone()[0] == 0:
            first_id = con.execute("SELECT id FROM domains ORDER BY id LIMIT 1").fetchone()[0]
            con.execute("UPDATE domains SET is_default = CASE WHEN id = ? THEN 1 ELSE 0 END", (first_id,))


def init_db(db_path: str) -> None:
    migrate(db_path)


def get_dashboard_context(app: Flask) -> dict:
    with sqlite3.connect(app.config["DATABASE"]) as con:
        con.row_factory = sqlite3.Row
        domains = con.execute(
            "SELECT id, hostname, is_default, created_at FROM domains ORDER BY is_default DESC, hostname ASC"
        ).fetchall()
        links = con.execute(
            """
            SELECT id, domain, custom_path, path_pattern, query_string, pdf_file_name, qr_file_name,
                   access_count, last_accessed, created_at
            FROM links
            ORDER BY created_at DESC
            """
        ).fetchall()

        enriched_links = []
        now = datetime.now(timezone.utc)
        week_ago = (now - timedelta(days=7)).isoformat()

        for row in links:
            link_id = row["id"]
            first_accessed_row = con.execute(
                "SELECT MIN(accessed_at) FROM link_access_log WHERE link_id = ?", (link_id,)
            ).fetchone()
            seven_days_row = con.execute(
                "SELECT COUNT(*) FROM link_access_log WHERE link_id = ? AND accessed_at >= ?",
                (link_id, week_ago),
            ).fetchone()
            top_referrer_row = con.execute(
                """
                SELECT referrer, COUNT(*) as total
                FROM link_access_log
                WHERE link_id = ? AND referrer IS NOT NULL AND referrer != ''
                GROUP BY referrer
                ORDER BY total DESC, referrer ASC
                LIMIT 1
                """,
                (link_id,),
            ).fetchone()

            live_url = build_live_url(row["domain"], row["path_pattern"], row["query_string"] or "")
            enriched_links.append(
                {
                    **dict(row),
                    "live_url": live_url,
                    "first_accessed": first_accessed_row[0] if first_accessed_row else None,
                    "last_7_days": seven_days_row[0] if seven_days_row else 0,
                    "top_referrer": top_referrer_row[0] if top_referrer_row else None,
                }
            )

        return {"domains": domains, "links": enriched_links}


def get_link_or_404(con: sqlite3.Connection, link_id: int) -> sqlite3.Row:
    row = con.execute(
        """
        SELECT id, domain, custom_path, path_pattern, query_string, pdf_file_name, qr_file_name,
               access_count, last_accessed, created_at
        FROM links WHERE id = ?
        """,
        (link_id,),
    ).fetchone()
    if row is None:
        abort(404)
    return row


def get_link_stats(con: sqlite3.Connection, link_id: int, access_count: int, last_accessed: str | None) -> dict:
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    first_accessed_row = con.execute(
        "SELECT MIN(accessed_at) FROM link_access_log WHERE link_id = ?", (link_id,)
    ).fetchone()
    seven_days_row = con.execute(
        "SELECT COUNT(*) FROM link_access_log WHERE link_id = ? AND accessed_at >= ?",
        (link_id, week_ago),
    ).fetchone()
    top_referrer_row = con.execute(
        """
        SELECT referrer, COUNT(*) as total
        FROM link_access_log
        WHERE link_id = ? AND referrer IS NOT NULL AND referrer != ''
        GROUP BY referrer
        ORDER BY total DESC, referrer ASC
        LIMIT 1
        """,
        (link_id,),
    ).fetchone()
    return {
        "total_access_count": access_count,
        "last_accessed": last_accessed,
        "first_accessed": first_accessed_row[0] if first_accessed_row else None,
        "last_7_days": seven_days_row[0] if seven_days_row else 0,
        "top_referrer": top_referrer_row[0] if top_referrer_row else None,
    }


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)

    root = Path(app.root_path)
    app.config.update(
        SECRET_KEY=os.getenv("SECRET_KEY", "dev-secret-key"),
        DATABASE=os.getenv("DATABASE", str(root / "instance" / "links.db")),
        UPLOAD_FOLDER=os.getenv("UPLOAD_FOLDER", str(root / "uploads")),
        QR_FOLDER=os.getenv("QR_FOLDER", str(root / "static" / "qrcodes")),
        MAX_CONTENT_LENGTH=int(os.getenv("MAX_CONTENT_LENGTH", str(50 * 1024 * 1024))),
        ALLOWED_IMPORT_ROOTS=os.getenv("ALLOWED_IMPORT_ROOTS", "/srv/pdfs:/home"),
        PORT=int(os.getenv("PORT", "8000")),
    )
    if test_config:
        app.config.update(test_config)

    os.makedirs(Path(app.config["DATABASE"]).parent, exist_ok=True)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(app.config["QR_FOLDER"], exist_ok=True)

    init_db(app.config["DATABASE"])

    @app.after_request
    def _apply_security_headers(response):
        return add_security_headers(response)

    def render_dashboard(status_code: int = 200):
        context = get_dashboard_context(app)
        return render_template("dashboard.html", **context), status_code

    def regenerate_qr(link_id: int, live_url: str) -> str:
        qr_file_name = f"{link_id}.png"
        qr_path = Path(app.config["QR_FOLDER"]) / qr_file_name
        qrcode.make(live_url).save(qr_path)
        return qr_file_name

    @app.get("/")
    @app.get("/dashboard")
    def dashboard():
        return render_dashboard()

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    @app.post("/domains")
    def create_domain():
        hostname = normalize_domain(request.form.get("hostname", ""))
        set_default = request.form.get("is_default") == "1"
        if not hostname:
            flash("Domain hostname is required.", "error")
            return render_dashboard(400)

        try:
            with sqlite3.connect(app.config["DATABASE"]) as con:
                if set_default:
                    con.execute("UPDATE domains SET is_default = 0")
                con.execute(
                    "INSERT INTO domains (hostname, is_default, created_at) VALUES (?, ?, ?)",
                    (hostname, 1 if set_default else 0, utc_now_iso()),
                )
                if not set_default and con.execute("SELECT COUNT(*) FROM domains WHERE is_default = 1").fetchone()[0] == 0:
                    new_id = con.execute("SELECT id FROM domains WHERE hostname = ?", (hostname,)).fetchone()[0]
                    con.execute("UPDATE domains SET is_default = CASE WHEN id = ? THEN 1 ELSE 0 END", (new_id,))
        except sqlite3.IntegrityError:
            flash("Domain already exists.", "error")
            return render_dashboard(409)

        flash("Domain added.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/domains/<int:domain_id>/delete")
    def delete_domain(domain_id: int):
        with sqlite3.connect(app.config["DATABASE"]) as con:
            con.row_factory = sqlite3.Row
            domain = con.execute("SELECT id, hostname, is_default FROM domains WHERE id = ?", (domain_id,)).fetchone()
            if domain is None:
                abort(404)

            in_use = con.execute("SELECT COUNT(*) FROM links WHERE domain = ?", (domain["hostname"],)).fetchone()[0]
            if in_use > 0:
                flash("Cannot delete domain because it is used by existing links.", "error")
                return render_dashboard(409)

            con.execute("DELETE FROM domains WHERE id = ?", (domain_id,))
            if domain["is_default"]:
                replacement = con.execute("SELECT id FROM domains ORDER BY id LIMIT 1").fetchone()
                if replacement:
                    con.execute("UPDATE domains SET is_default = CASE WHEN id = ? THEN 1 ELSE 0 END", (replacement[0],))

        flash("Domain deleted.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/links")
    def create_link():
        domain_id_raw = (request.form.get("domain_id", "") or "").strip()
        custom_path_input = request.form.get("custom_path", "")
        path_pattern, query_string = split_path_and_query(custom_path_input)
        validation_error = validate_path_pattern(path_pattern)
        if validation_error:
            flash(validation_error, "error")
            return render_dashboard(400)

        if not domain_id_raw.isdigit():
            flash("A valid domain selection is required.", "error")
            return render_dashboard(400)

        try:
            saved_pdf_name = save_pdf_from_inputs(app, require_pdf=True)
        except ValueError as exc:
            flash(str(exc), "error")
            return render_dashboard(400)

        try:
            with sqlite3.connect(app.config["DATABASE"]) as con:
                con.row_factory = sqlite3.Row
                domain = con.execute("SELECT id, hostname FROM domains WHERE id = ?", (int(domain_id_raw),)).fetchone()
                if domain is None:
                    flash("Selected domain does not exist.", "error")
                    return render_dashboard(400)

                cur = con.execute(
                    """
                    INSERT INTO links (domain, custom_path, path_pattern, query_string, pdf_file_name, created_at, access_count)
                    VALUES (?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        domain["hostname"],
                        path_pattern,
                        path_pattern,
                        query_string,
                        saved_pdf_name,
                        utc_now_iso(),
                    ),
                )
                link_id = cur.lastrowid
                live_url = build_live_url(domain["hostname"], path_pattern, query_string)
                qr_file_name = regenerate_qr(link_id, live_url)
                con.execute("UPDATE links SET qr_file_name = ? WHERE id = ?", (qr_file_name, link_id))
        except sqlite3.IntegrityError:
            Path(app.config["UPLOAD_FOLDER"], saved_pdf_name).unlink(missing_ok=True)
            flash("This domain + path already exists.", "error")
            return render_dashboard(409)

        flash("Link created.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/links/<int:link_id>/edit")
    def edit_link(link_id: int):
        domain_id_raw = (request.form.get("domain_id", "") or "").strip()
        custom_path_input = request.form.get("custom_path", "")
        path_pattern, query_string = split_path_and_query(custom_path_input)
        validation_error = validate_path_pattern(path_pattern)
        if validation_error:
            flash(validation_error, "error")
            return render_dashboard(400)

        if not domain_id_raw.isdigit():
            flash("A valid domain selection is required.", "error")
            return render_dashboard(400)

        try:
            new_pdf_name = save_pdf_from_inputs(app, require_pdf=False)
        except ValueError as exc:
            flash(str(exc), "error")
            return render_dashboard(400)

        try:
            with sqlite3.connect(app.config["DATABASE"]) as con:
                con.row_factory = sqlite3.Row
                link = get_link_or_404(con, link_id)
                domain = con.execute("SELECT hostname FROM domains WHERE id = ?", (int(domain_id_raw),)).fetchone()
                if domain is None:
                    flash("Selected domain does not exist.", "error")
                    return render_dashboard(400)

                old_pdf_name = link["pdf_file_name"]
                effective_pdf_name = new_pdf_name or old_pdf_name

                con.execute(
                    """
                    UPDATE links
                    SET domain = ?, custom_path = ?, path_pattern = ?, query_string = ?, pdf_file_name = ?
                    WHERE id = ?
                    """,
                    (domain[0], path_pattern, path_pattern, query_string, effective_pdf_name, link_id),
                )

                live_url = build_live_url(domain[0], path_pattern, query_string)
                qr_file_name = regenerate_qr(link_id, live_url)
                con.execute("UPDATE links SET qr_file_name = ? WHERE id = ?", (qr_file_name, link_id))

                if new_pdf_name and new_pdf_name != old_pdf_name:
                    Path(app.config["UPLOAD_FOLDER"], old_pdf_name).unlink(missing_ok=True)
        except sqlite3.IntegrityError:
            if new_pdf_name:
                Path(app.config["UPLOAD_FOLDER"], new_pdf_name).unlink(missing_ok=True)
            flash("This domain + path already exists.", "error")
            return render_dashboard(409)

        flash("Link updated.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/links/<int:link_id>/delete")
    def delete_link(link_id: int):
        with sqlite3.connect(app.config["DATABASE"]) as con:
            con.row_factory = sqlite3.Row
            link = get_link_or_404(con, link_id)
            con.execute("DELETE FROM link_access_log WHERE link_id = ?", (link_id,))
            con.execute("DELETE FROM links WHERE id = ?", (link_id,))

        Path(app.config["UPLOAD_FOLDER"], link["pdf_file_name"]).unlink(missing_ok=True)
        Path(app.config["QR_FOLDER"], f"{link_id}.png").unlink(missing_ok=True)

        flash("Link deleted.", "success")
        return redirect(url_for("dashboard"))

    def resolve_link_for_request(host: str, requested_path: str) -> tuple[sqlite3.Row | None, bool]:
        with sqlite3.connect(app.config["DATABASE"]) as con:
            con.row_factory = sqlite3.Row
            host_registered = (
                con.execute("SELECT 1 FROM domains WHERE hostname = ?", (host,)).fetchone() is not None
            )

            host_rows = con.execute(
                "SELECT id, domain, path_pattern, query_string, pdf_file_name, access_count, last_accessed FROM links WHERE domain = ?",
                (host,),
            ).fetchall()
            host_matches = [row for row in host_rows if match_path(row["path_pattern"], requested_path) is not None]
            exact_host_matches = [row for row in host_matches if normalize_path(row["path_pattern"]) == normalize_path(requested_path)]

            if len(exact_host_matches) == 1:
                return exact_host_matches[0], host_registered
            if len(host_matches) == 1:
                return host_matches[0], host_registered
            if len(host_matches) > 1:
                return None, host_registered

            if host_registered:
                return None, host_registered

            all_rows = con.execute(
                "SELECT id, domain, path_pattern, query_string, pdf_file_name, access_count, last_accessed FROM links"
            ).fetchall()
            fallback_matches = [row for row in all_rows if match_path(row["path_pattern"], requested_path) is not None]
            exact_fallback_matches = [
                row for row in fallback_matches if normalize_path(row["path_pattern"]) == normalize_path(requested_path)
            ]
            if len(exact_fallback_matches) == 1:
                return exact_fallback_matches[0], host_registered
            if len(fallback_matches) == 1:
                return fallback_matches[0], host_registered
            return None, host_registered

    def log_access_and_increment(link_id: int) -> None:
        query_json = json.dumps(request.args.to_dict(flat=False), ensure_ascii=False)
        with sqlite3.connect(app.config["DATABASE"]) as con:
            con.execute(
                """
                UPDATE links
                SET access_count = access_count + 1, last_accessed = ?
                WHERE id = ?
                """,
                (utc_now_iso(), link_id),
            )
            con.execute(
                """
                INSERT INTO link_access_log (link_id, accessed_at, ip, user_agent, query_params, referrer)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    link_id,
                    utc_now_iso(),
                    request.headers.get("X-Forwarded-For", request.remote_addr),
                    request.user_agent.string,
                    query_json,
                    request.referrer,
                ),
            )

    @app.get("/_file/<int:link_id>.pdf")
    def serve_pdf_file(link_id: int):
        with sqlite3.connect(app.config["DATABASE"]) as con:
            con.row_factory = sqlite3.Row
            link = get_link_or_404(con, link_id)

        pdf_path = Path(app.config["UPLOAD_FOLDER"]) / link["pdf_file_name"]
        if not pdf_path.exists():
            abort(404)

        as_attachment = request.args.get("download") == "1"
        response = send_file(
            pdf_path,
            mimetype="application/pdf",
            as_attachment=as_attachment,
            download_name=link["pdf_file_name"],
        )
        if not as_attachment:
            response.headers["Content-Disposition"] = f'inline; filename="{link["pdf_file_name"]}"'
        return response

    @app.get("/qr/<int:link_id>.png")
    def download_qr(link_id: int):
        size = request.args.get("size", "512")
        if size not in {"256", "512", "1024"}:
            abort(400, "size must be one of 256, 512, 1024")

        with sqlite3.connect(app.config["DATABASE"]) as con:
            con.row_factory = sqlite3.Row
            link = get_link_or_404(con, link_id)

        qr_path = Path(app.config["QR_FOLDER"]) / (link["qr_file_name"] or f"{link_id}.png")
        if not qr_path.exists():
            live_url = build_live_url(link["domain"], link["path_pattern"], link["query_string"] or "")
            regenerate_qr(link_id, live_url)

        with Image.open(qr_path) as img:
            resized = img.resize((int(size), int(size)))
            out = BytesIO()
            resized.save(out, format="PNG")
            out.seek(0)

        return send_file(
            out,
            mimetype="image/png",
            as_attachment=True,
            download_name=f"link-{link_id}-{size}.png",
        )

    @app.get("/links/<int:link_id>")
    def link_detail(link_id: int):
        with sqlite3.connect(app.config["DATABASE"]) as con:
            con.row_factory = sqlite3.Row
            link = get_link_or_404(con, link_id)
            logs = con.execute(
                """
                SELECT id, accessed_at, ip, user_agent, query_params, referrer
                FROM link_access_log
                WHERE link_id = ?
                ORDER BY accessed_at DESC
                LIMIT 100
                """,
                (link_id,),
            ).fetchall()
            stats = get_link_stats(con, link_id, link["access_count"], link["last_accessed"])

        return render_template("link_detail.html", link=link, logs=logs, stats=stats)

    @app.get("/api/links/<int:link_id>/stats")
    def link_stats_api(link_id: int):
        with sqlite3.connect(app.config["DATABASE"]) as con:
            con.row_factory = sqlite3.Row
            link = get_link_or_404(con, link_id)
            stats = get_link_stats(con, link_id, link["access_count"], link["last_accessed"])
        return jsonify({"link_id": link_id, **stats})

    @app.get("/<path:custom_path>")
    def serve_pdf(custom_path: str):
        requested_path = normalize_path(custom_path)
        host = request.host.split(":")[0].strip().lower()

        link, _ = resolve_link_for_request(host, requested_path)
        if link is None:
            abort(404)

        pdf_path = Path(app.config["UPLOAD_FOLDER"]) / link["pdf_file_name"]
        if not pdf_path.exists():
            abort(404)

        log_access_and_increment(link["id"])

        if request.args.get("download") == "1":
            return redirect(url_for("serve_pdf_file", link_id=link["id"], download=1))

        file_url = url_for("serve_pdf_file", link_id=link["id"], _external=True)
        viewer_url = f"https://mozilla.github.io/pdf.js/web/viewer.html?file={quote(file_url, safe='')}"
        return render_template("viewer.html", link=link, viewer_url=viewer_url)

    return app


app = create_app()


if __name__ == "__main__":
    desired = int(os.getenv("PORT", str(app.config.get("PORT", 8000))))
    bind_host = os.getenv("BIND_HOST", "127.0.0.1")
    port = find_free_port(desired, host=bind_host)
    Path(app.root_path, ".port").write_text(str(port), encoding="utf-8")
    ip = detect_local_ip()
    print(f"Starting server at http://{ip}:{port}")
    app.run(host=bind_host, port=port)
