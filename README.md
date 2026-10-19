# Marcelorian

*May the Force Hire You*

A Windows desktop pilot that helps one applicant find US ophthalmology
principal investigators and manage a 4-email outreach sequence. Every email is
approved individually, and the applicant presses Send in Outlook (the pilot's
only channel). Ordinary tested code controls approval, follow-ups, limits and
stopping; Claude suggestions for the personal lines are optional.

- **Testers:** [How to use Marcelorian](docs/TESTER_GUIDE.md) (also inside the zip)
- **Program owner:** [Build the tester zip and follow the pilot](docs/OWNER_SETUP.md)
- Specification: [`MARCELORIAN_SPEC.md`](MARCELORIAN_SPEC.md) (single source of rules)
- Decisions: [`docs/DECISIONS.md`](docs/DECISIONS.md)

## Status

Pilot build (P0–P5): profile and CV, lead list import and verification, free
PubMed/OpenAlex/NIH lookups, candidates, drafting with a deterministic
checklist, approval, Outlook hand-off, practice mode with simulated replies,
reply tags and tasks, a background worker with notices, owner exports,
backups/restore, delete-all-data, and a no-install Windows zip.

The app never sends email by itself and never contacts a PI on its own. Live
mode needs the real Outlook test on the tester's computer first
([runbook](docs/P0_3_WINDOWS_RUNBOOK.md)).

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

Run the app with `python -m marcelorian`. Tests use fake transports and
clients, an injected clock and temporary SQLite databases; they never use the
network or spend API credit.

## Privacy note

Local data lives in an unencrypted SQLite database under
`%LOCALAPPDATA%\Marcelorian\`, which Windows or OneDrive backup may copy.
Secrets (the optional Claude key) are kept only in Windows Credential Manager.
When Claude suggestions are on, the PI's public publication titles and selected
profile answers are sent to Anthropic, so not all data stays on the laptop then.
