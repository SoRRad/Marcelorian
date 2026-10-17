# P0.3 Windows transport test — redacted report

> **Redaction rules.** Never include full email addresses, names, OAuth
> tokens, client IDs or secrets, raw message headers, message bodies,
> screenshots of inboxes, or file paths that contain a user name. Refer to
> the two accounts only as **S** (sender) and **R2** (second consenting
> address). Write identifiers as patterns, for example `<32hex@sender-domain>`
> or `<CA…@mail.gmail.com>`, never as actual values. Keep the filled report out
> of the repository. Only this blank template is committed.

## 1. Run

| Field | Value |
|---|---|
| Date (UTC) | |
| Operator role | owner / applicant (no names) |
| Windows edition and exact build (`winver`) | |
| Python version | |
| App commit SHA tested | |
| OAuth consent-screen publishing status | Testing / In production |
| Sender account type (S) | consumer Gmail / Google Workspace |
| Second address type (R2) | consumer Gmail / Workspace / other |
| Real submissions made (total) | (must be ≤ 6) |

## 2. Results

Mark each item **pass**, **fail**, **not run** or **n/a**. Add a one-line note
for anything other than pass.

| # | Check | Result | Note |
|---|---|---|---|
| 0 | Local test suite on this laptop (`pytest -q`), including the 3 Windows adapter tests | | |
| 1 | Allowlist checklist completed before the first send | | |
| 2 | `connect`: granted scopes are exactly `gmail.send` + `gmail.readonly`; account matched | | |
| 3 | T1 self-send delivered (connectivity only) | | |
| 4 | T2 Email 1 S→R2 delivered; snapshot showed override, actual recipient and fake PI | | |
| 5 | T2 Email 2 in the same conversation (`threadId` matched) | | |
| 6 | `In-Reply-To` and `References` equal the actual RFC Message-ID of Email 1 | | |
| 7 | Subject exactly `Re: Research Position Application – Ophthalmology` (en dash, single `Re:`) at both S and R2 | | |
| 8 | Our `Message-ID` preserved by Gmail (if not: pattern Gmail used) | | |
| 9 | `X-Marcelorian-Id` preserved | | |
| 10 | If 8 failed: correlation via `X-Marcelorian-Id` / returned IDs worked | | |
| 11 | R2's reply observed through history sync, metadata only | | |
| 12 | History cursor saved only after all pages were processed | | |
| 13 | Follow-up after the reply was **not** sent | | |
| 14 | Every read request used `format=metadata` with the header allowlist; no `full`/`raw`/snippet/body/attachment request | | |
| 15 | No bodies, tokens or full addresses in app logs (spot-check `marcelorian.log`) | | |
| 16 | *(Optional, L2)* Revoked token → dispatch halted with "Gmail needs reconnecting" → reconnect → resumed | | |
| 17 | L3: token still valid after 7+ days | pending | Observe on or after: ____ (do not mark passed before then) |

## 3. Deviations and anomalies

Describe anything unexpected (for example a pause reason that appeared, a
warning screen, timing differences) **without** quoting message content or
addresses.

## 4. Not tested in this run

List what was skipped and why. For example: tray, toasts, sleep/resume and
auto-start are P3/P5 and out of scope; L3 is pending.

## 5. Cleanup

| Item | Done |
|---|---|
| Both allowlist entries deactivated | |
| Stored token removed (`disconnect`), or kept for L3 observation | |
| Local spike output kept only on the laptop | |
