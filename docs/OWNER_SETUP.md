# Owner setup: build the tester zip and follow the pilot

## 1. Build the zip (no install for the tester)

1. On GitHub, open **Actions → Package → Run workflow** (branch `main`).
2. Enter a version such as `v0.1.0-pilot`.
3. Leave **publish** off for a private test build. The zip appears under the run's
   **Artifacts** (visible to repository collaborators only).
4. To give a tester a download link, run it again with **publish** on. This creates a
   GitHub **pre-release** with the zip and its `.sha256`. In a public repository, that
   link is public; the zip holds only the app, no data. Publishing also marks every
   older pilot build as **superseded** (a notice at the top of its page linking to the
   new one), so testers never pick an old version by mistake.

The workflow uses Python 3.12.10 from `actions/setup-python`, installs every dependency
from `requirements/runtime-windows.txt` with hash checking, runs the app's self-test from
the packaged copy, and only then zips it. Windows runners are free for public
repositories; check your Actions allowance if the repository is private.

## 2. What to send the tester

- The release link (or the zip itself).
- Their lead list `.csv` (it stays out of the repository: it names real people).
- The guide: `docs/TESTER_GUIDE.md` (also inside the zip as *How to use Marcelorian.txt*).

## 3. Before the tester switches to live mode

- Run the real Outlook test on their computer (`docs/P0_3_WINDOWS_RUNBOOK.md`) and fill in
  the redacted report (`docs/P0_3_REPORT_TEMPLATE.md`).
- Then the tester ticks **"The real Outlook test (P0.3b) passed on this computer"** in
  Settings, together with the other live-readiness items.

## 4. Following progress (U4)

The tester sends you `owner_report.csv` and `status_summary.csv` from
**Settings → Your data**. They contain the allowlisted campaign columns and counts only:
no notes, CV, visa details, email addresses or correspondence.

## 5. Updating the tester

Publish a new version, and have the tester download and extract it, then start the new
folder. Their data lives in `%LOCALAPPDATA%\Marcelorian` (not in the folder), so it carries
over; the app backs it up before updating the database. The old folder can be deleted.

## 6. Claude suggestions (optional)

Off by default. If the tester enters their own Anthropic API key in Settings, each
"Suggest" click is one request billed to that key. Revoke it in the Anthropic console
when the pilot ends.
