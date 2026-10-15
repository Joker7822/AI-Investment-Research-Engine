from datetime import datetime, timezone

from fastapi import HTTPException

from app.api.public_v1 import _public_http_exception
from app.core.settings import Settings
from app.core.usage import (
    ResearchUsageCounters,
    ResearchUsageLimits,
    evaluate_research_usage,
)


def _limits(**overrides) -> ResearchUsageLimits:
    values = {
        "requests_per_minute": 3,
        "requests_per_day": 24,
        "work_units_per_day": 600,
        "max_work_units_per_request": 36,
    }
    values.update(overrides)
    return ResearchUsageLimits(**values)


def test_usage_allows_request_and_projects_counters():
    decision = evaluate_research_usage(
        _limits(),
        ResearchUsageCounters(minute_requests=1, daily_requests=4, daily_work_units=80),
        26,
        now=datetime(2026, 10, 15, 3, 20, 10, tzinfo=timezone.utc),
    )

    assert decision.allowed is True
    assert decision.reason == "allowed"
    assert decision.minute_requests == 2
    assert decision.daily_requests == 5
    assert decision.daily_work_units == 106


def test_usage_rejects_minute_burst_with_retry_after():
    decision = evaluate_research_usage(
        _limits(),
        ResearchUsageCounters(minute_requests=3, daily_requests=4, daily_work_units=80),
        20,
        now=datetime(2026, 10, 15, 3, 20, 42, tzinfo=timezone.utc),
    )

    assert decision.allowed is False
    assert decision.reason == "minute_request_limit"
    assert decision.limit == 3
    assert decision.retry_after_seconds == 18


def test_usage_rejects_daily_request_quota():
    decision = evaluate_research_usage(
        _limits(),
        ResearchUsageCounters(minute_requests=0, daily_requests=24, daily_work_units=100),
        20,
        now=datetime(2026, 10, 15, 3, 20, tzinfo=timezone.utc),
    )

    assert decision.allowed is False
    assert decision.reason == "daily_request_quota"
    assert decision.limit == 24
    assert decision.retry_after_seconds is not None


def test_usage_rejects_daily_agent_work_unit_quota():
    decision = evaluate_research_usage(
        _limits(),
        ResearchUsageCounters(minute_requests=0, daily_requests=5, daily_work_units=590),
        20,
        now=datetime(2026, 10, 15, 3, 20, tzinfo=timezone.utc),
    )

    assert decision.allowed is False
    assert decision.reason == "daily_work_unit_quota"
    assert decision.limit == 600


def test_usage_rejects_single_request_above_agent_cap_before_window_limits():
    decision = evaluate_research_usage(
        _limits(max_work_units_per_request=30),
        ResearchUsageCounters(minute_requests=0, daily_requests=0, daily_work_units=0),
        31,
    )

    assert decision.allowed is False
    assert decision.reason == "request_work_unit_limit"
    assert decision.limit == 30
    assert decision.retry_after_seconds is None


def test_zero_window_quotas_disable_only_window_limits():
    decision = evaluate_research_usage(
        _limits(requests_per_minute=0, requests_per_day=0, work_units_per_day=0),
        ResearchUsageCounters(minute_requests=999, daily_requests=999, daily_work_units=999999),
        20,
    )
    assert decision.allowed is True


def test_settings_default_to_bounded_research_usage():
    settings = Settings(_env_file=None)
    assert settings.api_research_rate_limit_per_minute == 3
    assert settings.api_research_daily_request_quota == 24
    assert settings.api_research_daily_agent_quota == 600
    assert settings.api_research_max_agents_per_request == 36


def test_public_contract_maps_internal_usage_limit_to_stable_rate_limited_error():
    internal = HTTPException(
        status_code=429,
        detail={
            "code": "research_daily_agent_quota_exceeded",
            "message": "Research usage limit reached",
            "details": {"reason": "daily_work_unit_quota", "limit": 600},
        },
        headers={"Retry-After": "3600"},
    )

    public = _public_http_exception(internal)
    assert public.status_code == 429
    assert public.detail["code"] == "RATE_LIMITED"
    assert public.detail["details"]["reason"] == "daily_work_unit_quota"
    assert public.headers == {"Retry-After": "3600"}
