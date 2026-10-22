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

- The release link (or the zip itself). Tell them to **Unblock** the zip before
  extracting it (tester guide, Part 1); otherwise Windows may block *Start Marcelorian*.
- Their lead list `.csv` (it stays out of the repository: it names real people).
- The guide: `docs/TESTER_GUIDE.md` (also inside the zip as *How to use Marcelorian.txt*).
- Which Outlook they use decides the best send option: Outlook on the web or *Copy the
  text* works for every account; the email file is for **classic** Outlook only.
- Optionally, your NCBI contact address (section 7) for **Settings → Publication lookups**.

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

## 7. Publication lookups: NCBI registration and optional keys

The app looks up PubMed, OpenAlex and NIH RePORTER without any account, paced below
each service's published limit (PubMed 3 requests a second, OpenAlex 10, RePORTER 1).

- **NCBI (PubMed):** every request names the tool `marcelorian`. NCBI asks the
  *developer* to register that tool name with a contact address, and may block heavy
  unregistered users. Register once by emailing `eutilities@ncbi.nlm.nih.gov` with the
  tool name `marcelorian` and an address you check, then give the tester that address
  to enter under **Settings → Publication lookups → NCBI contact address**. Use your own
  or a project address, not the tester's.
- **Optional keys** (free; each tester gets their own): an NCBI API key from their NCBI
  account settings (10 requests a second instead of 3), and an OpenAlex key from
  openalex.org (about 10,000 searches a day instead of about 1,000; OpenAlex no longer
  uses the old `mailto` "polite pool"). Both are kept in the Windows Credential Manager
  and removed by **Delete all my data**.
- **Still to confirm on the tester's laptop:** one real NIH RePORTER lookup (the
  `agencies: ["NEI"]` filter is documented but was not tried live) and one real OpenAlex
  lookup for a PI with an ORCID. The app refuses an OpenAlex profile whose name does not
  include the PI's last name (OpenAlex sometimes attaches an ORCID to the wrong record).
