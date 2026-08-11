# Local Docker Staging — Phase 1 Backend PR

Reviewer round 2 required Railway Staging OR "Docker test matrix with
deploy blocked until external staging exists". This document is the
Docker matrix — reproducible locally, no cloud creds needed.

Railway MCP requires the account owner's login and is not available
in the current session, so external Railway Staging is **BLOCKED**.
Deploy to production remains blocked per the standing rule.

## Build the image from the review branch

```bash
cd /path/to/pdfmasry-api
git checkout fix/phase1-security-hardening
docker build -t pdfmasry-api:staging .
```

## Run with staging-appropriate env (never production values)

Copy `scripts/staging.env.example` to `staging.env`, fill in **fresh
throwaway** secrets, then:

```bash
docker run --rm -p 8000:8000 --env-file staging.env pdfmasry-api:staging
```

Contents of `staging.env.example`:

```
APP_ENV=staging
DOWNLOAD_SECRET=<generate: openssl rand -hex 32>
ADMIN_STATS_TOKEN=<generate: openssl rand -hex 32>
CORS_EXTRA_ORIGINS=https://deploy-preview-1--pdfmasry-staging.netlify.app
ADOBE_MONTHLY_QUOTA=500
# USE_ADOBE unset  →  LibreOffice path (safe)
# CACHE_ENABLED is IGNORED  →  cache is hard-disabled in code
```

## Test matrix — copy-paste runbook

Every command below should be run against the local container
(`http://localhost:8000`). The matrix intentionally uses synthetic
files only — never real user data.

### 1. Health

```bash
curl -sf http://localhost:8000/health | grep '"status":"ok"'
```

### 2. Fail-loud: DOWNLOAD_SECRET missing in staging

```bash
docker run --rm -e APP_ENV=staging pdfmasry-api:staging
# expect exit 1 with:
# RuntimeError: DOWNLOAD_SECRET is not set. Refusing to boot ...
```

### 3. Valid Arabic PDF → Word (should return 200 + valid DOCX blob)

Generate the test PDF once:
```bash
python -c "
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
c = canvas.Canvas('/tmp/ar.pdf', pagesize=A4)
c.setFont('Helvetica', 20)
c.drawString(100, 700, 'PDFMasry staging test — Arabic PDF')
for i in range(10):
    c.drawString(100, 680 - i*20, f'Line {i+1}')
c.save()"

curl -s -F "file=@/tmp/ar.pdf" -w "\nHTTP: %{http_code}\n" \
     http://localhost:8000/api/pdf-to-word
# expect HTTP 200 + JSON with download_url. Follow the URL and confirm
# the returned bytes start with PK\x03\x04 (a valid DOCX zip).
```

### 4. Empty file → 400

```bash
: > /tmp/empty.pdf
curl -s -F "file=@/tmp/empty.pdf" -w "\nHTTP: %{http_code}\n" \
     http://localhost:8000/api/pdf-to-word
# expect HTTP 400, detail: "الملف فارغ"
```

### 5. Wrong MIME (JPG posing as PDF) → 415

```bash
convert -size 100x100 xc:red /tmp/img.jpg
curl -s -F "file=@/tmp/img.jpg;type=application/pdf" \
     -w "\nHTTP: %{http_code}\n" \
     http://localhost:8000/api/pdf-to-word
# expect HTTP 415, detail: "نوع الملف لا يتوافق مع هذه الأداة"
```

### 6. ZIP disguised as DOCX (no [Content_Types].xml) → 415

```bash
python -c "
import zipfile
with zipfile.ZipFile('/tmp/fake.docx', 'w') as z:
    z.writestr('random.txt', 'not a real docx')"
curl -s -F "file=@/tmp/fake.docx" -w "\nHTTP: %{http_code}\n" \
     http://localhost:8000/api/word-to-pdf
# expect HTTP 415, detail: "الملف ليس Office صالحاً"
```

### 7. Oversized upload → 413

```bash
dd if=/dev/zero of=/tmp/big.pdf bs=1M count=60
curl -s -F "file=@/tmp/big.pdf" -w "\nHTTP: %{http_code}\n" \
     http://localhost:8000/api/pdf-to-word
# expect HTTP 413, detail contains "الحد المسموح"
```

### 8. Rate limiting — 429 after 20 rapid requests

```bash
for i in $(seq 1 25); do
  curl -s -o /dev/null -w "%{http_code}\n" \
    -F "file=@/tmp/ar.pdf" \
    http://localhost:8000/api/pdf-to-word
done | tail -10
# expect several 429s appearing after ~20 successful 200s
```

### 9. request_id header round-trip

```bash
curl -sD- -F "file=@/tmp/empty.pdf" \
     http://localhost:8000/api/pdf-to-word 2>&1 | grep -i x-request-id
# expect: X-Request-ID: <32 hex chars>
# Every error response carries the same value in body {"request_id": ...}
```

### 10. Cleanup on error

```bash
# Upload something that fails, then check the temp_files dir
docker exec <container-id> ls -la /app/temp_files/
# expect the job dir cleaned up within 5-60s (background task runs).
```

### 11. Cache is truly dead

```bash
docker exec <container-id> ls -la /app/
# NO ./cache directory should exist, even after many requests.

docker exec <container-id> grep -r "pdf_cache" /app/main.py
# expect: no import; only a single reference inside admin cache-stats
# endpoint that returns a constant "disabled" shape.
```

### 12. Admin stats endpoint requires token

```bash
curl -s -w "\nHTTP: %{http_code}\n" \
     http://localhost:8000/api/admin/cache-stats
# expect HTTP 401 without X-Admin-Token header

curl -sH "X-Admin-Token: $ADMIN_STATS_TOKEN" \
     http://localhost:8000/api/admin/cache-stats
# expect HTTP 200 with counters block, cache_health: {disabled: true, ...}
```

## Expected outcome table

| # | Test | Expected | HTTP |
|---|---|---|---|
| 1 | health | `{"status":"ok"}` | 200 |
| 2 | boot without DOWNLOAD_SECRET (staging) | RuntimeError, exit 1 | — |
| 3 | valid PDF → docx | JSON with download_url | 200 |
| 4 | empty file | Arabic detail | 400 |
| 5 | JPG posing as PDF | Arabic detail | 415 |
| 6 | disguised zip | Arabic detail | 415 |
| 7 | 60MB upload | Arabic detail | 413 |
| 8 | 25 rapid uploads | some 429s | 200/429 |
| 9 | X-Request-ID | Present | 200/4xx |
| 10 | cleanup | job dir gone | — |
| 11 | no cache | zero cache dir/imports | — |
| 12 | admin token | 401 vs 200 | 401/200 |

## Why this doesn't fully substitute Railway staging

- **No real network conditions**: local loopback ≠ Railway's edge network.
- **No cross-region latency**: rate-limit thresholds and timeouts are
  validated in isolation, not under realistic load.
- **No X-Forwarded-For behavior**: our rate limiter keys on
  `get_remote_address` which reads X-F-F when running behind Railway
  proxy. Locally it sees 127.0.0.1 for every request — matrix test 8
  proves the limiter is wired, but Railway-specific behavior needs a
  real deploy to verify.
- **No Deploy Preview → staging CORS integration test**: requires both
  services running with the same env variables the reviewer specified.

Until Railway MCP creds are provided (or the owner sets up staging
manually and shares the URL), production deploy remains blocked per
the standing governance rule.
