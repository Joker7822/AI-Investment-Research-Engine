from __future__ import annotations

import argparse
import asyncio
import json

from app.core.settings import get_settings
from app.metrics.edinet_silver import extract_silver_metrics
from app.persistence.edinet import EdinetIndexRepository
from app.providers.edinet import EdinetApiClient
from app.providers.edinet_csv import parse_edinet_csv_package


FETCHABLE_LEGAL_STATUSES = {"1", "2"}


async def sync(doc_id: str) -> dict[str, object]:
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required for EDINET CSV sync")
    if not settings.edinet_api_key:
        raise RuntimeError("EDINET_API_KEY is required for EDINET CSV sync")

    repository = EdinetIndexRepository(settings.database_url, settings.database_schema_path)
    client = EdinetApiClient(settings.edinet_api_key, settings.edinet_timeout_seconds)
    try:
        filing = await repository.get_csv_eligible_filing(doc_id)
        if filing is None:
            raise ValueError(f"EDINET filing is not indexed: {doc_id}")
        if not filing.get("csv_flag"):
            raise ValueError(f"EDINET filing has csvFlag=0: {doc_id}")
        if str(filing.get("legal_status") or "") not in FETCHABLE_LEGAL_STATUSES:
            raise ValueError(
                f"EDINET filing is not in a fetchable legalStatus (1 or 2): {doc_id}"
            )

        payload = await client.fetch_csv_package(doc_id)
        parsed = parse_edinet_csv_package(payload)
        mapping_version, accounting_standard, silver = extract_silver_metrics(parsed.facts)
        changed = await repository.store_csv_and_silver(doc_id, parsed, silver)

        result = {
            "status": "stored" if changed else "unchanged_source_rebuilt_silver",
            "doc_id": doc_id,
            "sha256": parsed.sha256,
            "files": len(parsed.files),
            "facts": len(parsed.facts),
            "accounting_standard": accounting_standard,
            "mapping_version": mapping_version,
            "silver_metrics": len(silver),
        }
        print(json.dumps(result, ensure_ascii=False))
        return result
    finally:
        await repository.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch one EDINET type=5 package and atomically rebuild Bronze + deterministic Silver metrics"
    )
    parser.add_argument("--doc-id", required=True, help="EDINET document ID, e.g. S100XXXX")
    args = parser.parse_args()
    asyncio.run(sync(args.doc_id))


if __name__ == "__main__":
    main()
