from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.core.settings import get_settings
from app.persistence.edinet import EdinetIndexRepository
from app.providers.edinet import EdinetApiClient


TOKYO = ZoneInfo("Asia/Tokyo")


def _target_date(value: str | None) -> date:
    if value:
        return date.fromisoformat(value)
    return datetime.now(TOKYO).date()


async def sync(target_date: date) -> int:
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required for EDINET index sync")
    if not settings.edinet_api_key:
        raise RuntimeError("EDINET_API_KEY is required for EDINET index sync")

    repository = EdinetIndexRepository(
        settings.database_url,
        settings.database_schema_path,
    )
    client = EdinetApiClient(
        settings.edinet_api_key,
        settings.edinet_timeout_seconds,
    )

    await repository.start_sync(target_date)
    try:
        documents = await client.fetch_document_list(target_date)
        await repository.upsert_filings(target_date, documents)
        await repository.complete_sync(target_date, len(documents))
    except Exception as exc:
        await repository.fail_sync(target_date, f"{exc.__class__.__name__}: {exc}")
        raise
    finally:
        await repository.close()

    print(
        json.dumps(
            {
                "status": "completed",
                "date": target_date.isoformat(),
                "documents": len(documents),
            },
            ensure_ascii=False,
        )
    )
    return len(documents)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync one EDINET document-list date into PostgreSQL")
    parser.add_argument(
        "--date",
        dest="date_value",
        help="EDINET file date in YYYY-MM-DD format. Defaults to today's Asia/Tokyo date.",
    )
    args = parser.parse_args()
    asyncio.run(sync(_target_date(args.date_value)))


if __name__ == "__main__":
    main()
