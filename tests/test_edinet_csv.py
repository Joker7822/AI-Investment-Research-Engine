import csv
import io
import zipfile

import httpx
import pytest

from app.providers.edinet import EdinetApiClient
from app.providers.edinet_csv import EXPECTED_HEADERS, parse_edinet_csv_package


def _package(rows, *, filename="XBRL_TO_CSV/jpcrp.csv", bom=True):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter="\t", lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(EXPECTED_HEADERS)
    writer.writerows(rows)
    data = stream.getvalue().encode("utf-16" if bom else "utf-16le")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(filename, data)
    return buffer.getvalue()


def test_parse_official_nine_column_utf16_tsv():
    payload = _package(
        [
            (
                "jppfs_cor:CashAndDeposits",
                "現金及び預金",
                "CurrentYearInstant",
                "当期末",
                "連結",
                "時点",
                "JPY",
                "円",
                "200000",
            ),
            (
                "jpcrp_cor:BusinessRisksTextBlock",
                "事業等のリスク",
                "FilingDateInstant",
                "提出日時点",
                "その他",
                "時点",
                "",
                "",
                "複数行\nテキスト",
            ),
        ]
    )
    parsed = parse_edinet_csv_package(payload)

    assert len(parsed.files) == 1
    assert len(parsed.facts) == 2
    assert parsed.facts[0].element_id == "jppfs_cor:CashAndDeposits"
    assert parsed.facts[0].context_id == "CurrentYearInstant"
    assert parsed.facts[0].value == "200000"
    assert parsed.facts[1].value == "複数行\nテキスト"
    assert len(parsed.sha256) == 64


def test_parser_accepts_utf16le_without_bom():
    payload = _package(
        [("a", "b", "c", "d", "e", "f", "g", "h", "i")],
        bom=False,
    )
    assert parse_edinet_csv_package(payload).facts[0].value == "i"


def test_parser_rejects_archive_without_official_csv_tree():
    payload = _package(
        [("a", "b", "c", "d", "e", "f", "g", "h", "i")],
        filename="../escape.csv",
    )
    with pytest.raises(ValueError, match="no XBRL_TO_CSV"):
        parse_edinet_csv_package(payload)


def test_parser_rejects_wrong_header_layout():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "XBRL_TO_CSV/bad.csv",
            "要素ID\t値\nfoo\tbar\n".encode("utf-16"),
        )
    with pytest.raises(ValueError, match="Unexpected EDINET CSV headers"):
        parse_edinet_csv_package(buffer.getvalue())


def test_parser_enforces_uncompressed_size_limit():
    payload = _package(
        [("a", "b", "c", "d", "e", "f", "g", "h", "i")]
    )
    with pytest.raises(ValueError, match="uncompressed-size limit"):
        parse_edinet_csv_package(payload, max_uncompressed_bytes=10)


async def test_api_client_fetches_type5_zip():
    payload = _package(
        [("a", "b", "c", "d", "e", "f", "g", "h", "i")]
    )
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = EdinetApiClient("secret", client=http_client)
        result = await client.fetch_csv_package("S100TEST")

    assert result == payload
    assert seen[0].url.path == "/api/v2/documents/S100TEST"
    assert seen[0].url.params["type"] == "5"
    assert seen[0].url.params["Subscription-Key"] == "secret"
