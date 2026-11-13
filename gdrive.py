"""Google Drive uploader for statement connectors.

Mirrors the local archive layout into a Drive folder:

    My Drive/Statements/Utilities/AT&T Fiber/2026/2026-10-12 AT&T Fiber bill.pdf

Uses the `drive.file` scope, which only lets these scripts see and change files
and folders they created themselves, not the rest of your Drive.

One-time setup:  python gdrive.py --auth   (opens a browser to approve access)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
HOME = Path.home() / ".statement-connectors"
CLIENT_SECRETS = HOME / "google-client.json"   # downloaded from Google Cloud Console
TOKEN = HOME / "google-token.json"             # written by --auth, refreshed automatically

FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveNotAuthorized(Exception):
    pass


def _q(s: str) -> str:
    """Escape a value for a Drive search query."""
    return s.replace("\\", "\\\\").replace("'", "\\'")


def load_credentials(interactive: bool = False) -> Credentials:
    creds = None
    if TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif interactive:
        if not CLIENT_SECRETS.exists():
            raise DriveNotAuthorized(
                f"Missing {CLIENT_SECRETS}. See README: 'Google Drive setup'."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS), SCOPES)
        creds = flow.run_local_server(port=0)
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
    ap.add_argument("--auth", action="store_true", help="approve Drive access in a browser")
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
