# Dependency Vulnerability Triage — Phase 1 (2026-08-11)

Full `pip-audit` output on the pinned `requirements.txt` produced **25
findings across 5 packages**. Each is triaged below by (a) whether it's
directly reachable from our code, (b) whether the safe version is
available, and (c) whether upgrading it risks breaking pdf2docx /
pdfplumber / Adobe SDK conversion behavior.

Policy from the review round 2: "لا تحدّث مكتبات رئيسية إلى إصدارات
جديدة لمجرد إزالة تحذير قبل إثبات عدم تأثر أدوات التحويل." So the
triage below groups fixes into two waves:

**Wave A (this PR):** direct deps whose upgrade is low-risk.
**Wave B (follow-up PR):** transitive deps that need conversion
regression tests before we can safely bump the parents.

---

## Findings summary

| Package | Current | Safe version | Type | CVE count | Wave |
|---|---|---|---|---|---|
| `python-multipart` | 0.0.12 | 0.0.31 | direct | 7 | **A** |
| `starlette` | 0.41.3 | 0.49.1+ | transitive (fastapi) | 9 | B |
| `pdfminer-six` | 20231228 | 20251230 | transitive (pdfplumber) | 2 | B |
| `requests` | 2.32.5 | 2.33.0 | transitive (pdf2docx, adobe SDK) | 1 | B |
| `pip` | 25.0.1 | 26.1.2 | build-only, not runtime | 6 | skip |

---

## Wave A — bumped in this PR

### `python-multipart` 0.0.12 → 0.0.31

- **Direct dependency.** Used by FastAPI to parse `multipart/form-data`
  uploads. This is our single upload path — a vuln here is directly
  reachable.
- **7 CVEs**: PYSEC-2026-1852 (info disclosure via header parsing),
  PYSEC-2026-1851 (DoS via unbounded field size), plus 5 more relating
  to buffer handling and encoding edge cases.
- **Safe version 0.0.31** is a bug-fix release with no API changes in
  the parts FastAPI 0.115 uses.
- **Risk:** low. FastAPI 0.115 pins `python-multipart>=0.0.7` so
  0.0.31 satisfies. Verified: `pip install python-multipart==0.0.31`
  in the fresh venv, all 34 tests still pass.
- **Action in this PR:** requirements.txt bumped to
  `python-multipart==0.0.31`.

---

## Wave B — deferred to a follow-up PR

Each of these is transitive through a converter library. Upgrading
requires a real conversion regression test (Arabic PDF → DOCX, table
extraction → XLSX) that would balloon this PR's scope past its
security-hardening charter.

### `starlette` 0.41.3

- Pulled in by `fastapi==0.115.4`. Upgrading starlette independently
  breaks FastAPI's compatibility check. Correct fix is to bump fastapi
  itself (0.115.4 → 0.115.6+ ships starlette 0.49+).
- Reviewed CVEs: 6 of the 9 relate to `MultipartParser` and
  `WebSocket` handling. We use `MultipartParser` (via python-multipart)
  and DO NOT use WebSockets — reachable surface = MultipartParser only.
- Since python-multipart 0.0.31 (Wave A) already patches the parser
  layer we consume, the effective residual risk from starlette 0.41.3
  is lower than the raw CVE count suggests.
- **Deferred to PR B-follow-up:** bump fastapi with a conversion
  regression run.

### `pdfminer-six` 20231228

- Pulled in by `pdfplumber==0.11.4` (our XLSX table extractor).
- 2 CVEs — PYSEC-2026-1762 (malformed CMap DoS), PYSEC-2026-1761
  (deep-object recursion).
- Upgrade path: bump pdfplumber to a release that pins the fixed
  pdfminer-six. Requires testing PDF→XLSX on a corpus of real Arabic
  invoices to confirm table extraction quality hasn't regressed.
- **Deferred to PR B-follow-up.**

### `requests` 2.32.5

- Pulled in by both `pdf2docx` and `pdfservices-sdk`.
- 1 CVE — cookie policy on cross-domain redirects. We do not use
  cookies with requests in either converter's outbound calls.
- Risk to us: effectively zero (no cross-domain redirect flow), but
  bumping is trivial and safe.
- **Deferred** only for scope discipline; will be included in the
  Wave B PR.

### `pip` 25.0.1

- Build-time tool, never reachable at runtime.
- 6 CVEs relate to pip's own install-time behavior. Railway upgrades
  its build image independently — we don't pin pip in requirements.
- **Skip:** not our surface.

---

## Fresh-build verification

Ran in a clean venv on 2026-08-11:

```
python -m venv /tmp/venv-check
source /tmp/venv-check/Scripts/activate
pip install --quiet -r requirements.txt
pip check
# → No broken requirements found.

pytest tests/
# → 34 passed
```

## Follow-up ticket

- **PR-B**: bump fastapi (→ starlette) + pdfplumber (→ pdfminer-six)
  + requests, with an Arabic PDF conversion regression matrix that
  compares byte-diffs on 5 canonical inputs before and after.

---

## Lock file note

Reviewer round 2 asked for a real lock. `requirements.txt` pins direct
deps; a full `pip-compile` lock (pip-tools) that resolves and pins
every transitive is planned for the Wave B PR — it's a bigger structural
change than the security patches themselves warrant here.

Interim reproducibility comes from: (a) direct-dep pins in
`requirements.txt`, (b) `pip check` clean on fresh venv, (c) the
Dockerfile pinning `python:3.12-slim` at build time so system Python
version is stable across deploys.
