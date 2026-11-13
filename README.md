# Statement connectors

## Setup (once)

```bash
python3 -m venv ~/.statement-connectors/venv
source ~/.statement-connectors/venv/bin/activate
pip install -r requirements.txt
playwright install chromium        # on a bare Linux server: playwright install --with-deps chromium
```

Every step below works from a terminal alone, including over SSH or on a headless server.

Keep `att_fiber.py`, `filing.py` and `gdrive.py` in the same folder.

## Google Drive setup (once, optional)

Uploads mirror the local layout into `My Drive/Statements/Utilities/AT&T Fiber/2026/…`.
The scripts use Google's `drive.file` permission, so they can only see files and folders
they created, not the rest of your Drive.

1. Go to <https://console.cloud.google.com/>, create a project (e.g. "Statement connectors").
2. **APIs & Services → Library**: enable **Google Drive API**.
3. **APIs & Services → OAuth consent screen**: choose *External*, fill in the app name and
   your email, and add yourself under **Test users**.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**, type
   **TVs and Limited Input devices**. Download the JSON and save it as
   `~/.statement-connectors/google-client.json`.
   (An older *Desktop app* client won't work with this flow; create a new one of this type.)
5. Run `python gdrive.py --auth`. It prints a URL (`google.com/device`) and a short code.
   Open that URL on any phone or computer, enter the code, sign in and approve. The script
   notices the approval by itself and saves the token.

Steps 1 to 4 are a one-time setup in the Cloud Console and can be done from any device.

Add `--drive` to any fetch to upload new statements (`--drive "Other Folder"` to use a
different top-level folder). Local copies are always kept; a failed upload is retried on the
next run.

> While the consent screen is in *Testing* mode, Google expires the sign-in after 7 days.
> To avoid re-running `--auth` weekly, click **Publish app** on the consent screen. It stays
> private to you; Google just shows an "unverified app" warning the first time you approve.

**No-code alternative:** if you use the Google Drive for desktop app, point `--dest` at a
folder inside your synced Drive instead and skip all of the above.

## AT&T Fiber

1. **Sign in once:** `python att_fiber.py --login` (optionally `--user you@example.com`)
   Everything happens in the terminal: it asks for your AT&T user ID, your password (hidden
   prompt, never saved), where to send the verification code if AT&T asks, and then the code.
   A headless browser fills in AT&T's sign-in form and ticks "remember this device".
   Your password is not stored anywhere; only AT&T's session cookie is kept in
   `~/.statement-connectors/profiles/att/`.
2. **Fetch bills:** `python att_fiber.py --dest ~/Documents/Statements`
   New bills land in `~/Documents/Statements/Utilities/AT&T Fiber/2026/2026-10-12 AT&T Fiber bill.pdf`.
   Point `--dest` at a Dropbox or Google Drive folder to sync them.
3. **Schedule it** (macOS/Linux, 8:15am on the 20th of each month):
   ```
   15 8 20 * * cd /path/to/connectors && ~/.statement-connectors/venv/bin/python att_fiber.py --dest ~/Documents/Statements --drive >> ~/.statement-connectors/att.log 2>&1
   ```

## If something goes wrong

| Exit code | Meaning | Fix |
|---|---|---|
| 2 | Session expired, or `--login` couldn't finish | Run `--login` again. If it still fails, it saved `login.png` and `login.html` to `~/.statement-connectors/state/att_debug/` |
| 3 | No bill links found | Run `--debug`; screenshot and HTML go to `~/.statement-connectors/state/att_debug/` |
| 4 | Google Drive not authorized or sign-in expired | Run `python gdrive.py --auth` |

The debug snapshots are what's needed to update the page patterns (near the top of
`att_fiber.py`) if AT&T redesigns its site.
