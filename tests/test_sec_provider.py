from datetime import datetime, timezone

from app.domain.schemas import AnalysisRequest, AssetRef
from app.providers.sec import SecCompanyFactsProvider, extract_metrics


def companyfacts_payload():
    return {
        "entityName": "Example Corp",
        "facts": {
            "us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {
                        "USD": [
                            {
                                "val": 100,
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "filed": "2026-02-10",
                                "form": "10-K",
                                "accn": "0000000000-26-000001",
                            },
                            {
                                "val": 999,
                                "start": "2026-01-01",
                                "end": "2026-09-30",
                                "filed": "2026-11-01",
                                "form": "10-Q",
                                "accn": "0000000000-26-000099",
                            },
                        ]
                    }
                },
                "Assets": {
                    "units": {
                        "USD": [
                            {
                                "val": 500,
                                "end": "2026-06-30",
                                "filed": "2026-08-01",
                                "form": "10-Q",
                                "accn": "0000000000-26-000050",
                            }
                        ]
                    }
                },
            }
        },
    }


def test_extract_metrics_excludes_future_filings():
    as_of = datetime(2026, 10, 14, tzinfo=timezone.utc)
    metrics, evidence = extract_metrics(companyfacts_payload(), as_of, "0000000000")
    assert metrics["revenue"]["value"] == 100
    assert metrics["revenue"]["filed"] == "2026-02-10"
    assert metrics["assets"]["value"] == 500
    assert all(e.observed_at is None or e.observed_at <= as_of for e in evidence)


def test_sec_provider_skips_non_us_equity():
    provider = SecCompanyFactsProvider("Example App contact@example.com")
    request = AnalysisRequest(
        as_of=datetime(2026, 10, 14, tzinfo=timezone.utc),
        asset=AssetRef(type="equity", ticker="7203", exchange="TSE", region="Japan"),
        user_question="Analyze",
    )
    assert provider._is_us_equity(request) is False
