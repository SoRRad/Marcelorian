# P0.3 Windows transport test — runbook

**Status: not started. Needs the owner's separate written approval.** Nothing
in this runbook may be run before that approval. This runbook covers spec
§18 P0.3 and LIVE check L1 (with optional L2). L3 (token still valid after
7+ days) is only *observed* later; it is never declared passed early.

## What this test is

A small, owner-assisted check that the real Gmail path behaves the way the
mocked tests assume. It runs in `transport_test` mode on one Windows 11
laptop, between **two email addresses that the testers control and have
consented to use**. Every message is synthetic. No principal investigator,
no real applicant CV, and no real applicant data are involved.

What it checks:

1. **Self-send:** the app can send through Gmail at all (connectivity only).
2. **Two-address thread:** Email 1 goes to the second address, then a
   follow-up goes in the same thread. Checked: `threadId`, `In-Reply-To` and
   `References`, the exact subject with the en dash, and a single `Re:`.
3. **Identifiers:** whether Gmail keeps our `Message-ID` and the
   `X-Marcelorian-Id` header, checked by reading **metadata only**, and that
   Email 2's `In-Reply-To`/`References` equal the Message-ID Gmail stored for
   Email 1 (provider-observed), with the app recording
   `thread_headers_verified` for each send. Before this run, rewritten IDs were
   only simulated with a fake transport (M06).
4. **Reply handling:** the second address replies. The app sees the reply
   through history sync using metadata only, and the next follow-up is
   **not** sent.
5. **Metadata only:** every read request uses `format=metadata` and the
   header allowlist. `full`, `raw`, snippets, bodies and attachments are
   never requested.
6. **History sync:** the cursor is saved only after every page of a range
   has been processed.
7. *(Optional, L2)* **Reconnect:** after the token is revoked, dispatch
   stops with "Gmail needs reconnecting", and resumes after reconnecting with
   the **same** Google account. Once transport_test history exists, connecting
   a different account is refused and changes nothing (M02).
8. *(Optional)* **Sleep between claim and send:** not reproducible on demand
   with a real laptop; the gateway refuses to call Gmail when the hand-off
   after the claim exceeds 5 s or crosses the window end (M01). Note any
   `not_submitted` attempt with resolution `not_invoked` in the report.

At most **6 real submissions** in total, far below the 25-per-24-hours budget.

## What gets delivered at the gate (after approval)

So nothing exists that could send mail before you approve, these parts are
written only after approval, and you review them before running anything:

- the real Gmail adapter, using the same `SendGateway` as the tests (no
  separate spike sender)
- a small launcher, `python -m marcelorian.spike`, with the subcommands
  `preflight`, `connect`, `t1-self-send`, `t2-thread`, `t3-reply-check`,
  `metadata-check`, `report` and `disconnect`
- a synthetic one-page PDF to use as the test attachment

Each sending subcommand shows the full approval snapshot (sender, the actual
test recipient, the fake PI, subject, body, attachment hash, and the visible
transport-test schedule override). It sends only after you type the first 8
characters of the snapshot's SHA-256. Live mode cannot be selected: the
readiness checklist is not complete, and the app rejects schedule overrides
in live mode.

## Before the session: what you prepare on the laptop

Keep all of this **on the laptop**. Do not paste any of it into chat, email or
the repository.

| Item | Where it lives | Notes |
|---|---|---|
| Windows 11 edition and build | `winver` | Record the exact build in the report. |
| Python 3.12 (64-bit) | python.org installer | `py -3.12 --version` |
| Google Cloud project with the Gmail API enabled | owner's console | Desktop OAuth client. Publishing status **In production** avoids the Testing-mode 7-day refresh-token expiry; record which status was used. |
| Desktop OAuth client file (`client_secret_*.json`) | `%LOCALAPPDATA%\Marcelorian\spike\` | Never inside the repository. It is configuration, not a mailbox token, but it still stays untracked. |
| Sender account (S) | Gmail | The account that authorizes `gmail.send` + `gmail.readonly` only. |
| Second consenting address (R2) | Gmail or other | Someone in the test who can reply from it. |

## Recipient allowlist checklist (complete before any send)

- [ ] The allowlist contains **exactly two** addresses: S (self-send) and R2.
- [ ] The person behind each address has agreed, in writing, to receive synthetic test emails.
- [ ] Neither address belongs to a principal investigator, a university contact, or anyone outside the test.
- [ ] Neither address appears in the app's PI records. The app rejects this as well (decision B11); the operator confirms it anyway.
- [ ] R2 has no auto-forwarding rule that would pass test mail to anyone else.
- [ ] The two people will not email each other about anything else during the session. Unrelated mail between the two addresses would show up as "prior contact" or a "possible reply" and pause the test. That is the intended conservative behavior, but it adds noise.
- [ ] The fake PI record is marked **synthetic**, and its own address is a non-deliverable placeholder (for example `fake-pi@synthetic-univ.invalid`).
- [ ] The message body starts with the banner `MARCELORIAN TRANSPORT TEST — synthetic message, no action needed`, and the attachment is the synthetic PDF, never the real CV.
- [ ] After the session, both allowlist entries are deactivated in the app.

## Steps (after approval)

1. **Get the reviewed code.** Use the commit named in the approval (its
   SHA-256 checksum is published with the bundle). Create a virtual
   environment and install with hashes:
   `py -3.12 -m venv .venv`, then
   `.venv\Scripts\pip install --require-hashes -r requirements\lock.txt`, then
   `.venv\Scripts\pip install --no-deps -e .`
2. **Run the tests locally first:** `.venv\Scripts\pytest -q`. All tests must
   pass, including the three Windows adapter tests (Credential Manager, known
   folders, instance lock). If any fail, stop and report.
3. **`preflight`** (no network). This checks the Windows build, Python,
   `tzdata`, Credential Manager, and that the app is in `transport_test`. It
   also checks that the allowlist has exactly the two consenting addresses.
4. **`connect`**. A browser opens the Google consent screen. Grant
   **only** "send email on your behalf" and "view your email messages and
   settings". The tool then checks that the account and scopes match and
   stores the token in Windows Credential Manager. If Google shows an
   "unverified app" warning, confirm that it names the owner's project before
   continuing.
5. **`t1-self-send`**: Email 1 from S to S. Confirm it arrives. This proves
   connectivity only.
6. **`t2-thread`**:
   - Email 1 from S to R2, with the transport-test override (window always
     open, 10-minute interval), bound into the snapshot.
   - After 10 minutes, approve and send Email 2. It must appear in the same
     Gmail conversation for both addresses, with subject
     `Re: Research Position Application – Ophthalmology`.
7. **R2 replies.** Use Gmail's normal **Reply** on Email 2 and do not edit the
   subject. Use any short sentence, and do not include personal information.
8. **`t3-reply-check`**. The tool runs a metadata-only history sync, records
   the reply, and then tries to dispatch Email 3. Expected: **no send**.
   Email 3 is never drafted (the reply stopped the sequence), or, if a draft
   exists, dispatch pauses it. The terminal shows which.
9. **`metadata-check`**. This reads the sent messages with `format=metadata`
   and the header allowlist. It reports whether `Message-ID` and
   `X-Marcelorian-Id` were preserved, and prints the request parameters
   (never the message content).
10. *(Optional L2)* Revoke the app at
    <https://myaccount.google.com/permissions>, then try a dispatch. Expected:
    dispatch halts with "Gmail needs reconnecting" and nothing is sent.
    Run `connect` again, and dispatch resumes.
11. **`report`** writes a redacted draft of
    [`P0_3_REPORT_TEMPLATE.md`](P0_3_REPORT_TEMPLATE.md) to
    `%LOCALAPPDATA%\Marcelorian\spike\`. Check it, edit anything that looks
    identifying, then share **only that file**.
12. **Clean up:** deactivate both allowlist entries. Optionally run
    `disconnect` to delete the stored token. If L3 (7-day token survival) is
    going to be observed, keep the token and note the date.

## P0.3b Outlook hand-off check (separate approval)

The primary channel is the Outlook hand-off (spec §9.8, decisions S1–S8): the app
never touches Outlook, it prepares each email and you send it yourself. This check
needs its own written approval and runs in `transport_test` between your Outlook
address (**S**) and a second consenting address (**R2**), with synthetic text only.
The Gmail steps above are not needed for it.

Setup: connect the Outlook hand-off mailbox with S's address, complete the
signature step (add a synthetic signature or skip), and allowlist R2 only.

1. **Link, Email 1 to R2:** click **Open in Outlook**. Check that a new message
   opens with To, the exact subject (with the en dash) and the body filled in, and
   that Outlook added its own signature. Send it from Outlook, then click **Sent**.
2. **File:** for a second test campaign, click **Download email file** and open it.
   Check: it opens as an editable new message, the app signature is at the end,
   the synthetic CV is attached, and there is no From line to fill. Send it, then
   click **Sent**.
3. **Follow-up by reply and paste:** when step 2 of campaign 1 is due (a
   transport_test schedule override may shorten the wait), open the earlier email
   in Sent Items, click **Reply**, paste the copied body, send, then click **Sent**.
   Check in R2's mailbox that it arrived in the same conversation with one `Re:`.
4. **Follow-up by file (thread test, owner decision 2026-10-19):** for campaign 2's
   step 2, use **Download email file** instead and send it. Record whether R2's
   mailbox shows it in the same conversation or as a new one. Either result is
   fine; it decides whether the app recommends reply-and-paste only.
5. **Not sent:** hand off a third test email, close it without sending, click
   **Not sent** (after the confirmation). Check that the message needs
   re-approval and nothing reached R2.
6. **Reply:** from R2, reply to campaign 1. In the app click **PI replied**. Check
   that the campaign moves to conversation and no further step is offered.

At most 6 real sends in total. Record results in section 6 of the report template.

## Do not

- Do not send to, or put in the allowlist, any address that is not one of the two consenting test addresses.
- Do not paste tokens, the OAuth client file, raw headers containing addresses, or screenshots of inboxes into chat or into the repository.
- Do not disable Windows Defender or SmartScreen, or bypass organizational controls.
- Do not use the applicant's real CV or profile.
- Do not mark L3 as passed before 7 full days have elapsed.
