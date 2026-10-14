# BillsCollector

Collects your monthly invoices from your email and the sites you download them
from, files them per month, and sends the lot to your accountant. It runs on
your Mac with a simple local UI. Your passwords stay in the macOS Keychain and
your invoices stay on your disk.

## Start it

1. Get this repo onto your Mac, e.g. `git clone https://github.com/vladg96/BillsCollector`.
2. Double-click **`BillsCollector.command`**. The first time, it sets itself up,
   which takes about a minute. Then the app opens in your browser at
   http://127.0.0.1:8765.
   - If macOS says it's from an unidentified developer, right-click the file → **Open**.
   - It needs Python 3.10 or newer. If you don't have it: `brew install python`.
3. Leave the Terminal window open while you use the app. Close it to stop.

## Set it up once

**Connections:** where invoices come from.
- **Apple Mail on this Mac** covers every account already in Mail.app (work,
  Gmail, Yahoo, …) with no extra password. macOS protects Mail's folder, so turn
  on *System Settings → Privacy & Security → Full Disk Access* for **Terminal**,
  then restart Terminal.
- **Gmail / Yahoo / Outlook / iCloud / other**: sign in with an *app password*.
  This is a separate password that only allows mail access and can be revoked
  any time. Each form links to where you create it. Use these for mailboxes
  that aren't in Mail.app.

**Vendors:** who sends bills, e.g. *Digi, sender contains `@digi.ro`*. Every
connection is searched for every vendor.
- The app takes the PDF attachments and can follow "download your invoice"
  links.
- Vendors are expected every month by default, so you're told when one is
  missing.

**Revolut Business (read-only):** Connections → *Revolut Business*. A 3-step
wizard:
1. The app creates a certificate. You paste it in Revolut Business →
   *Settings → APIs → Business API*.
2. You paste back the Client ID.
3. You approve read-only access.

After that, each month it generates a **PDF and a CSV statement per currency
account**, built from the API (not Revolut's stamped version). The month page
also lists **card payments that have no invoice**, so you can add the invoice
or mark the merchant "No invoice needed". Access is READ-only: the app cannot
make payments. Your private key and access token stay in the Keychain.

**Checklist:** anything else you download by hand each month.
- It appears under *Still needed* with a link and a file picker.
- The file you pick is renamed and filed into the month.

**Settings:**
- Your accountant's address and message.
- How to send:
  - **Apple Mail**: opens a ready email with everything attached, and you
    press Send.
  - **Directly**: sends from one of your mailbox connections.
- **Automatic monthly run**: on the day you pick, the Mac collects last
  month's invoices and notifies you. If the Mac is asleep that day, it runs
  when it wakes. For this run to read Apple Mail, also give Full Disk Access to
  the Python path shown on the Settings page.

## Every month

Open **Months**, click **Collect now** (or let the monthly run do it), then:
1. Add whatever *Still needed* lists.
2. Check the files.
3. Click **Email to accountant**.

Nothing is sent to your accountant without you clicking it.

Invoices are saved in `~/Library/Application Support/BillsCollector/invoices/<YYYY-MM>/`.

## Command line

```bash
.venv/bin/python -m billscollector ui                     # the app
.venv/bin/python -m billscollector run --month 2026-09    # collect a month
.venv/bin/python -m billscollector check                  # test all connections
```

## Coming next

- **Website logins.** Click *Connect*, log in once in a browser window, and the
  app downloads invoices from that portal each month (`collectors/portal.py`).

## Running headless on GitHub Actions (optional)

`.github/workflows/collect-invoices.yml` can run the mailbox/API part on a
schedule without your Mac. It uses a `config.yaml` in the repo root (see
`config.example.yaml`), and `${NAME}` placeholders in it are filled from repo
secrets. Keep the repo private if you use this.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest -q
```
