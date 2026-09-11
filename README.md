# qr-mac-project

A Flask dashboard to create domain-based PDF links, generate QR codes, and track usage analytics.

## Features

- Multi-domain management from dashboard (add/list/delete)
- Link creation using a registered domain + custom path/query/template
- PDF rendering in-browser with PDF.js viewer
- Raw PDF sub-route with `?download=1` support
- QR code generated per link and downloadable as PNG
- Access analytics + detailed access logs + JSON stats API
- PDF import from:
  - browser upload
  - server-side absolute path (restricted by allowed roots)
- Lightweight SQLite schema migrations for existing databases
- One-command VPS start script with free-port auto-selection

## Quickstart

```bash
bash run.sh
```

Then open the printed URL.

## Manual run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

## Environment variables

Copy `.env.example` and export values as needed.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE` | `instance/links.db` | SQLite DB path |
| `UPLOAD_FOLDER` | `uploads` | Stored PDF files |
| `QR_FOLDER` | `static/qrcodes` | Stored QR PNG files |
| `PORT` | `8000` | Preferred start port |
| `MAX_CONTENT_LENGTH` | `52428800` | Max file size (bytes, 50MB default) |
| `ALLOWED_IMPORT_ROOTS` | `/srv/pdfs:/home` | Allowed server import roots (colon-separated) |
| `SECRET_KEY` | `dev-secret-key` | Flask flash/session key |
| `ADMIN_USER` | `admin` | Admin login username (demo default) |
| `ADMIN_PASSWORD` | `admin123` | Admin login password (demo default; change it) |

## VPS deployment notes

1. Upload project to VPS.
2. Run `bash run.sh`.
3. (Optional) Install systemd unit:
   ```bash
   sudo cp deploy/qr-mac.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now qr-mac
   ```
4. (Optional) Nginx reverse proxy:

```nginx
server {
    listen 80;
    server_name your-domain.example;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## API

- `GET /api/links/<id>/stats` returns JSON stats for a link.

## Tests

```bash
pytest
```

## বাংলা সংক্ষেপ

এই প্রজেক্টে ড্যাশবোর্ড থেকে ডোমেইন ম্যানেজ করে PDF লিংক + QR তৈরি করা যায়, আর ব্যবহার পরিসংখ্যান দেখা যায়।
