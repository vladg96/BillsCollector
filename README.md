# BillsCollector

Every month, gathers your invoices from your mailboxes, APIs and billing
portals, renames and dedupes them, and emails them to your accountant. Runs on
GitHub Actions.

If a vendor you expect every month is missing, or a source fails, your
accountant gets nothing. You get an **"Action needed"** email instead, with
everything found so far and a list of what to fix.

## How it works

| Source | What it handles | Config `type` |
| --- | --- | --- |
| Mailbox | PDF attached to the email; email with a "download invoice" link. Works with Gmail, Yahoo, Outlook or any IMAP mailbox, and you can add as many mailboxes as you like. | `imap` |
| Stripe | Invoices you issued to your own clients | `stripe` |
| Any URL | An API or link that returns the month's PDF | `http` |
| Portal | A website where you log in and click download (headless browser) | `plugin` + `collectors.portal` |
| Anything else | Your own Python function `collect(period, cfg) -> list[Invoice]` | `plugin` |

Each mail rule has a `vendor` and `from` (a sender substring). It can also take
a `subject` (regex), `links` (regex for download links) and `attachments`
(true/false). Mail is read-only: it is never marked as read or moved.

A **catch-all** rule picks up mail you forward to yourself with `[bills]` in
the subject. Use it for anything that can't be automated, such as portals with
2FA on every login or paper bills you scanned.

Files are named `2026-09-03_Vodafone_factura.pdf`. Large bundles are split over
several emails, kept under 18 MB each by default.

## Setup

1. **Keep this repo private.** The run artifacts contain your invoices.
2. `cp config.example.yaml config.yaml` and fill in your vendors.
   - Not sure what a vendor's emails look like? Open one and copy the sender
     domain into `from`.
   - If you'd rather not commit `config.yaml`, paste its contents into a
     secret named `BILLS_CONFIG` instead.
3. Create **app passwords**. Both Gmail and Yahoo need 2-step verification
   turned on first.
   - Gmail: https://myaccount.google.com/apppasswords
   - Yahoo: Account security → Generate app password
4. Add each `${NAME}` used in the config as a repo secret under
   *Settings → Secrets and variables → Actions*, for example:
   `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `YAHOO_USER`, `YAHOO_APP_PASSWORD`,
   `ACCOUNTANT_EMAIL`. Every secret is exposed to the config automatically.
5. Go to *Actions → Collect invoices → Run workflow*, pick last month and set
   `send = no`. Check the job summary and the `invoices` artifact. Tweak the
   rules until nothing is missing.

After that it runs by itself on the **5th of every month** and collects the
previous month by date received.

### Run options (manual runs)

- `send = auto`: emails your accountant only if everything is there.
- `send = no`: builds the zip only.
- `send = force`: sends even if something is missing.
- `month`: any past month, e.g. `2026-07`.

## Local use

```bash
pip install -r requirements.txt
export GMAIL_USER=... GMAIL_APP_PASSWORD=...   # whatever your config uses
python -m billscollector check                 # validate config + secrets
python -m billscollector run --month 2026-09 --no-send
python -m pytest -q
```

For portal collectors you also need
`pip install playwright && python -m playwright install chromium`.
The workflow installs Playwright automatically when the config uses it.
