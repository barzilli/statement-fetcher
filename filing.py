"""Shared filing helpers for statement connectors.

Every connector hands a downloaded PDF to `file_statement`, which validates it,
names it consistently and places it in the archive:

    <dest>/<category>/<provider>/<YYYY>/<YYYY-MM-DD> <provider> <kind>.pdf

A small JSON state file per provider remembers what has already been filed, so
re-running a connector never downloads or files the same statement twice.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import date, datetime
from pathlib import Path

MONTHS = {
    m: i
    for i, names in enumerate(
        [
            (),
            ("jan", "january"), ("feb", "february"), ("mar", "march"),
            ("apr", "april"), ("may",), ("jun", "june"), ("jul", "july"),
            ("aug", "august"), ("sep", "sept", "september"),
            ("oct", "october"), ("nov", "november"), ("dec", "december"),
        ]
    )
    for m in names
}

_MONTH_NAME_DATE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})\b",
    re.IGNORECASE,
)
_NUMERIC_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b")
_MONTH_YEAR = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(\d{4})\b",
    re.IGNORECASE,
)


def parse_statement_date(text: str) -> date | None:
    """Pull the first plausible date out of free text such as a bill-history row."""
    if not text:
        return None
    if m := _MONTH_NAME_DATE.search(text):
        mon, day, year = m.groups()
        return _safe_date(int(year), MONTHS[mon.lower()], int(day))
    if m := _NUMERIC_DATE.search(text):
        mon, day, year = (int(g) for g in m.groups())
        year += 2000 if year < 100 else 0
        return _safe_date(year, mon, day)
    if m := _MONTH_YEAR.search(text):
        mon, year = m.groups()
        return _safe_date(int(year), MONTHS[mon.lower()], 1)
    return None


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def is_pdf(path: Path) -> bool:
    with open(path, "rb") as fh:
        return fh.read(5) == b"%PDF-"


class StatementArchive:
    """Local archive of statements, optionally mirrored to Google Drive.

    `drive` is any object with `upload(local_path, folders) -> file_id`
    (normally gdrive.DriveUploader). Files are always filed locally first;
    `upload_pending()` then pushes anything not yet on Drive, so an upload that
    fails today is retried automatically on the next run.
    """

    def __init__(self, dest: Path, category: str, provider: str, state_dir: Path,
                 drive=None):
        self.dest = Path(dest).expanduser()
        self.category = category
        self.provider = provider
        self.drive = drive
        state_dir = Path(state_dir).expanduser()
        state_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", provider.lower()).strip("-")
        self.state_path = state_dir / f"{slug}.json"
        self.state = self._load()

    # ---- state -----------------------------------------------------------
    def _load(self) -> dict:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text())
        return {"filed": {}}

    def _save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2, sort_keys=True))
        tmp.replace(self.state_path)

    def has(self, key: str) -> bool:
        return key in self.state["filed"]

    # ---- filing ----------------------------------------------------------
    def target_path(self, stmt_date: date, kind: str = "statement") -> Path:
        name = f"{stmt_date.isoformat()} {self.provider} {kind}.pdf"
        name = re.sub(r'[<>:"/\\|?*]', "", name)  # safe on Windows too
        return self.dest / self.category / self.provider / str(stmt_date.year) / name

    def file_statement(self, tmp_pdf: Path, stmt_date: date, key: str,
                       kind: str = "statement") -> Path:
        if not is_pdf(tmp_pdf):
            raise ValueError(f"{tmp_pdf} is not a PDF (site may have returned an error page)")
        target = self.target_path(stmt_date, kind)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():  # same date, different statement (e.g. corrected bill)
            n = 2
            while (alt := target.with_name(f"{target.stem} ({n}).pdf")).exists():
                n += 1
            target = alt
        shutil.move(str(tmp_pdf), target)
        self.state["filed"][key] = {
            "path": str(target),
            "statement_date": stmt_date.isoformat(),
            "filed_at": datetime.now().isoformat(timespec="seconds"),
            "drive_id": None,
        }
        self._save()
        return target

    # ---- Google Drive ----------------------------------------------------
    def upload_pending(self, log=print) -> tuple[int, int]:
        """Upload every filed statement that isn't on Drive yet. Returns (uploaded, failed)."""
        if not self.drive:
            return 0, 0
        uploaded = failed = 0
        for key, rec in sorted(self.state["filed"].items()):
            if rec.get("drive_id"):
                continue
            local = Path(rec["path"])
            if not local.exists():
                log(f"Drive: local file missing, skipping {local}")
                failed += 1
                continue
            folders = list(local.relative_to(self.dest).parts[:-1])
            try:
                rec["drive_id"] = self.drive.upload(local, folders)
                rec["uploaded_at"] = datetime.now().isoformat(timespec="seconds")
                self._save()
                uploaded += 1
                log(f"Drive: uploaded {'/'.join(folders + [local.name])}")
            except Exception as e:  # leave it pending; next run retries
                failed += 1
                log(f"Drive: upload failed for {local.name}: {e}")
        return uploaded, failed
