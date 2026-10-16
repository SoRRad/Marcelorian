# Marcelorian

*May the Force Hire You*

A Windows desktop pilot that helps one applicant find US ophthalmology
principal investigators and manage a 4-email outreach sequence through the
applicant's own Gmail. Every email is approved individually by the applicant.
AI researches and drafts; ordinary tested code controls approval, sending,
follow-ups, limits and stopping.

- Specification: [`MARCELORIAN_SPEC.md`](MARCELORIAN_SPEC.md) (single source of rules)
- Decisions after the v2 review: [`docs/DECISIONS.md`](docs/DECISIONS.md)

## Status

Phase **P0.1–P0.2** (safe foundation): settings, database and migrations, OS
credential store, local UI session security, and the sending safety core
(approval snapshots, reservations and caps, the 24-hour budget, pacing,
windows and due dates, the single send gateway with atomic claim,
reconciliation and the uncertain state). Real Gmail is **not** connected yet;
the P0.3 Windows transport test needs separate owner authorization
([runbook](docs/P0_3_WINDOWS_RUNBOOK.md)).

Nothing in this phase sends email, contacts a PI, or calls a paid API.

## Development

Requires Python 3.12.

```
python -m venv .venv
.venv/bin/pip install --require-hashes -r requirements/lock.txt   # Windows: .venv\Scripts\pip
.venv/bin/pip install --no-deps -e .
.venv/bin/pytest
.venv/bin/ruff check src tests
.venv/bin/mypy
```

Tests use a fake Gmail transport, an injected clock and temporary SQLite
databases. They never use the network.

## Privacy note

Local data lives in an unencrypted SQLite database under
`%LOCALAPPDATA%\Marcelorian\`, which Windows or OneDrive backup may copy.
Secrets are kept only in Windows Credential Manager. Use of Google user data
will follow Google's API Services User Data Policy (Limited Use).
