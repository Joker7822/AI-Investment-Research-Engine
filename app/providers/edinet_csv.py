from __future__ import annotations

import csv
import hashlib
import io
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath


EXPECTED_HEADERS = (
    "要素ID",
    "項目名",
    "コンテキストID",
    "相対年度",
    "連結・個別",
    "期間・時点",
    "ユニットID",
    "単位",
    "値",
)


@dataclass(frozen=True)
class EdinetCsvFact:
    source_file: str
    row_number: int
    element_id: str
    item_name: str
    context_id: str
    relative_year: str
    consolidation: str
    period_type: str
    unit_id: str
    unit_label: str
    value: str


@dataclass(frozen=True)
class ParsedEdinetCsvPackage:
    sha256: str
    files: tuple[str, ...]
    facts: tuple[EdinetCsvFact, ...]


def _safe_member_name(name: str) -> bool:
    path = PurePosixPath(name.replace("\\", "/"))
    return (
        not path.is_absolute()
        and ".." not in path.parts
        and len(path.parts) >= 2
        and path.parts[0].upper() == "XBRL_TO_CSV"
        and path.suffix.lower() == ".csv"
    )


def _decode_csv(data: bytes) -> str:
    # Official EDINET viewer documentation specifies UTF-16LE. `utf-16`
    # handles a BOM when present; explicit LE handles packages without one.
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    return data.decode("utf-16le")


def parse_edinet_csv_package(
    payload: bytes,
    *,
    max_files: int = 64,
    max_uncompressed_bytes: int = 256 * 1024 * 1024,
    max_file_bytes: int = 64 * 1024 * 1024,
    max_rows: int = 1_000_000,
) -> ParsedEdinetCsvPackage:
    """Parse an EDINET type=5 ZIP without extracting files to disk.

    The limits are defenses against malformed archives / ZIP bombs. Only files
    directly under the official XBRL_TO_CSV tree and ending in `.csv` are read.
    """

    if not payload.startswith(b"PK"):
        raise ValueError("EDINET CSV response is not a ZIP archive")

    package_hash = hashlib.sha256(payload).hexdigest()
    facts: list[EdinetCsvFact] = []
    accepted_files: list[str] = []

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = [m for m in archive.infolist() if not m.is_dir() and _safe_member_name(m.filename)]
        if not members:
            raise ValueError("EDINET ZIP contains no XBRL_TO_CSV/*.csv files")
        if len(members) > max_files:
            raise ValueError("EDINET ZIP exceeds CSV file-count limit")
        if sum(m.file_size for m in members) > max_uncompressed_bytes:
            raise ValueError("EDINET ZIP exceeds uncompressed-size limit")

        for member in members:
            if member.file_size > max_file_bytes:
                raise ValueError(f"EDINET CSV file exceeds size limit: {member.filename}")
            raw = archive.read(member)
            text = _decode_csv(raw)
            reader = csv.DictReader(io.StringIO(text), delimiter="\t")
            headers = tuple(reader.fieldnames or ())
            if headers != EXPECTED_HEADERS:
                raise ValueError(
                    f"Unexpected EDINET CSV headers in {member.filename}: {headers!r}"
                )

            accepted_files.append(member.filename)
            for row_number, row in enumerate(reader, start=2):
                if len(facts) >= max_rows:
                    raise ValueError("EDINET CSV package exceeds row limit")
                facts.append(
                    EdinetCsvFact(
                        source_file=member.filename,
                        row_number=row_number,
                        element_id=row.get("要素ID") or "",
                        item_name=row.get("項目名") or "",
                        context_id=row.get("コンテキストID") or "",
                        relative_year=row.get("相対年度") or "",
                        consolidation=row.get("連結・個別") or "",
                        period_type=row.get("期間・時点") or "",
                        unit_id=row.get("ユニットID") or "",
                        unit_label=row.get("単位") or "",
                        value=row.get("値") or "",
                    )
                )

    return ParsedEdinetCsvPackage(
        sha256=package_hash,
        files=tuple(accepted_files),
        facts=tuple(facts),
    )
