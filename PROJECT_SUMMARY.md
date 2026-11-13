# Statement Fetcher: project summary

A personal replacement for FileThis (shut down in 2023): scripts that sign in to
providers, download new statements, file them with consistent names, and copy
them to Google Drive.

Last updated: November 13, 2026

## Status

| Piece | State |
|---|---|
| Shared filing module (`filing.py`) | Built and tested |
| Google Drive upload (`gdrive.py`) | Built; upload code not yet run against real Drive |
| AT&T Fiber connector (`att_fiber.py`) | Built; not yet run against the real AT&T site |
| Other providers | Not started |

## How it works

1. **Connector (one per provider).** A Playwright browser script that opens the
   provider's billing page using a saved browser profile, finds bill PDF links,
   and downloads any not already filed.
2. **Filing (`filing.py`).** Checks the file is really a PDF, names it, and saves:
   `<dest>/<Category>/<Provider>/<YYYY>/<YYYY-MM-DD> <Provider> <kind>.pdf`
   e.g. `Utilities/AT&T Fiber/2026/2026-10-12 AT&T Fiber bill.pdf`.
   A JSON file per provider in `~/.statement-connectors/state/` records what's been
   filed so nothing is downloaded twice.
3. **Drive upload (`gdrive.py`, `--drive` flag).** Mirrors the same folder layout
   under `My Drive/Statements/`. Local copies are always kept; failed uploads stay
   pending and are retried on the next run.

## Key decisions

- **No stored passwords.** The user signs in once with `--login` in a visible
  browser and ticks "remember this device". Later runs reuse that session.
  Exit code 2 means it expired: run `--login` again.
- **Selectors match on visible text** ("bill history", "View PDF", "Download")
  rather than page code, to survive minor site changes. If AT&T redesigns,
  run `--headed --debug` and use the saved screenshot/HTML to update the
  patterns near the top of `att_fiber.py`.
- **Drive uses the `drive.file` permission**, so the scripts can only see files
  they created, not the rest of Drive. Setup needs a Google Cloud project and a
  Desktop OAuth client (steps in `README.md`). Publish the consent screen to avoid
  the 7-day sign-in expiry of testing mode.
- **Simpler Drive alternative:** point `--dest` at a folder synced by Google
  Drive for desktop and skip the Cloud setup.
- **Run monthly** via cron (example in `README.md`).

## Exit codes

0 ok · 2 provider session expired · 3 no bill links found · 4 Drive not authorized

## Adding a new provider

Copy `att_fiber.py` and change: `PROVIDER`, `CATEGORY`, `KIND`, the URLs,
the link-text patterns, and the profile folder name. Filing, de-duplication and
Drive upload come from `filing.py` and `gdrive.py` unchanged. Where a provider
offers OFX/Direct Connect or an API, prefer that over browser automation.
Bills that arrive as email attachments are better fetched from email than scraped.

## Next steps

1. First real AT&T run: `python att_fiber.py --login`, then
   `python att_fiber.py --headed --debug`. Share the debug snapshot if no bills
   are found.
2. Set up Drive (`python gdrive.py --auth`) and test `--drive`.
3. Pick the next providers.

## Files

- `att_fiber.py`: AT&T Fiber connector
- `filing.py`: naming, folder layout, de-duplication, Drive retry tracking
- `gdrive.py`: Google Drive uploader and `--auth` setup
- `README.md`: install, Drive setup, scheduling, troubleshooting
- `requirements.txt`: Python dependencies
