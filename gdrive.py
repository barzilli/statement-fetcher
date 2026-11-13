"""Google Drive uploader for statement connectors.

Mirrors the local archive layout into a Drive folder:

    My Drive/Statements/Utilities/AT&T Fiber/2026/2026-10-12 AT&T Fiber bill.pdf

Uses the `drive.file` scope, which only lets these scripts see and change files
and folders they created themselves, not the rest of your Drive.

One-time setup:  python gdrive.py --auth
It prints a short code and a URL. Open the URL on any phone or computer, enter
the code and approve. Nothing on this machine needs a web browser, so it works
over SSH or on a headless server (Google's "limited-input device" flow, which
supports the drive.file scope).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
HOME = Path.home() / ".statement-connectors"
CLIENT_SECRETS = HOME / "google-client.json"   # "TVs and Limited Input devices" client from Cloud Console
TOKEN = HOME / "google-token.json"             # written by --auth, refreshed automatically

DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveNotAuthorized(Exception):
    pass


def _q(s: str) -> str:
    """Escape a value for a Drive search query."""
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _client_info() -> tuple[str, str]:
    if not CLIENT_SECRETS.exists():
        raise DriveNotAuthorized(
            f"Missing {CLIENT_SECRETS}. See README: 'Google Drive setup'."
        )
    data = json.loads(CLIENT_SECRETS.read_text())
    info = data.get("installed") or data.get("web") or data
    try:
        return info["client_id"], info["client_secret"]
    except KeyError:
        raise DriveNotAuthorized(f"{CLIENT_SECRETS} has no client_id/client_secret.")


def device_flow() -> Credentials:
    """Authorize with a code typed on any other device; no browser needed here."""
    client_id, client_secret = _client_info()
    r = requests.post(DEVICE_CODE_URL, data={"client_id": client_id, "scope": " ".join(SCOPES)},
                      timeout=30)
    if r.status_code != 200:
        raise DriveNotAuthorized(
            f"Google refused the device-code request ({r.status_code}): {r.text.strip()}\n"
            "The OAuth client must be of type 'TVs and Limited Input devices'. See README."
        )
    dc = r.json()
    print(
        "\nOn any phone or computer, open:\n"
        f"    {dc['verification_url']}\n"
        f"and enter the code:\n"
        f"    {dc['user_code']}\n"
        "Then sign in with your Google account and approve. Waiting…\n",
        flush=True,
    )
    interval = int(dc.get("interval", 5))
    deadline = time.monotonic() + int(dc.get("expires_in", 1800))
    while time.monotonic() < deadline:
        time.sleep(interval)
        t = requests.post(TOKEN_URL, data={
            "client_id": client_id,
            "client_secret": client_secret,
            "device_code": dc["device_code"],
            "grant_type": DEVICE_GRANT,
        }, timeout=30)
        body = t.json()
        if t.status_code == 200:
            return Credentials(
                token=body["access_token"],
                refresh_token=body.get("refresh_token"),
                token_uri=TOKEN_URL,
                client_id=client_id,
                client_secret=client_secret,
                scopes=SCOPES,
                # google-auth compares expiry against naive UTC.
                expiry=(datetime.now(timezone.utc).replace(tzinfo=None)
                        + timedelta(seconds=int(body.get("expires_in", 3600)))),
            )
        err = body.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        if err == "access_denied":
            raise DriveNotAuthorized("Access was declined on the approval page.")
        raise DriveNotAuthorized(f"Google sign-in failed: {err}: {body.get('error_description', '')}")
    raise DriveNotAuthorized("The code expired before it was approved. Run  python gdrive.py --auth  again.")


def load_credentials(interactive: bool = False) -> Credentials:
    creds = None
    if TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if creds and creds.valid and not interactive:
        return creds
    if creds and creds.expired and creds.refresh_token and not interactive:
        try:
            creds.refresh(Request())
        except RefreshError:
            raise DriveNotAuthorized(
                "Google Drive sign-in expired or was revoked. Run:  python gdrive.py --auth"
            )
    elif interactive:
        creds = device_flow()
    else:
        raise DriveNotAuthorized("Google Drive not authorized. Run:  python gdrive.py --auth")
    TOKEN.parent.mkdir(parents=True, exist_ok=True)
    TOKEN.write_text(creds.to_json())
    TOKEN.chmod(0o600)
    return creds


class DriveUploader:
    def __init__(self, root_name: str = "Statements"):
        self.root_name = root_name
        self.svc = build("drive", "v3", credentials=load_credentials(), cache_discovery=False)
        self._folders: dict[tuple[str, str], str] = {}

    def _find(self, name: str, parent: str, mime: str | None = None) -> str | None:
        q = f"name = '{_q(name)}' and '{parent}' in parents and trashed = false"
        if mime:
            q += f" and mimeType = '{mime}'"
        res = self.svc.files().list(q=q, fields="files(id)", pageSize=1,
                                    spaces="drive").execute(num_retries=3)
        files = res.get("files", [])
        return files[0]["id"] if files else None

    def _folder(self, name: str, parent: str) -> str:
        key = (parent, name)
        if key not in self._folders:
            fid = self._find(name, parent, FOLDER_MIME)
            if not fid:
                fid = self.svc.files().create(
                    body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent]},
                    fields="id",
                ).execute(num_retries=3)["id"]
            self._folders[key] = fid
        return self._folders[key]

    def upload(self, local_path: Path, folders: list[str]) -> str:
        """Upload `local_path` into root/<folders...>/, skipping if it's already there."""
        parent = self._folder(self.root_name, "root")
        for name in folders:
            parent = self._folder(name, parent)
        existing = self._find(local_path.name, parent)
        if existing:
            return existing
        media = MediaFileUpload(str(local_path), mimetype="application/pdf", resumable=True)
        return self.svc.files().create(
            body={"name": local_path.name, "parents": [parent]},
            media_body=media,
            fields="id",
        ).execute(num_retries=3)["id"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--auth", action="store_true", help="approve Drive access by entering a code on any other device")
    a = ap.parse_args()
    if not a.auth:
        ap.print_help()
        return 0
    try:
        load_credentials(interactive=True)
    except DriveNotAuthorized as e:
        print(e)
        return 1
    print(f"Authorized. Token saved to {TOKEN}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
