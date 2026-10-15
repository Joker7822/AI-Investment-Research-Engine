from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal


UsageLimitReason = Literal[
    "allowed",
    "minute_request_limit",
    "daily_request_quota",
    "daily_work_unit_quota",
    "request_work_unit_limit",
]


@dataclass(frozen=True)
class ResearchUsageLimits:
    requests_per_minute: int
    requests_per_day: int
    work_units_per_day: int
    max_work_units_per_request: int


@dataclass(frozen=True)
class ResearchUsageCounters:
    minute_requests: int = 0
    daily_requests: int = 0
    daily_work_units: int = 0


@dataclass(frozen=True)
class ResearchUsageDecision:
    allowed: bool
    reason: UsageLimitReason
    work_units: int
    minute_requests: int
    daily_requests: int
    daily_work_units: int
    limit: int | None = None
    retry_after_seconds: int | None = None


def _seconds_until_next_minute(now: datetime) -> int:
    return max(1, 60 - now.second)


def _seconds_until_next_utc_day(now: datetime) -> int:
    utc_now = now.astimezone(timezone.utc)
    next_day = utc_now.replace(hour=0, minute=0, second=0, microsecond=0)
    next_day = next_day.replace(day=utc_now.day)  # keep mypy/linters simple before timedelta
    from datetime import timedelta

    next_day += timedelta(days=1)
    return max(1, int((next_day - utc_now).total_seconds()))


def evaluate_research_usage(
    limits: ResearchUsageLimits,
    counters: ResearchUsageCounters,
    work_units: int,
    *,
    now: datetime | None = None,
) -> ResearchUsageDecision:
    """Evaluate a new research request against deterministic usage limits.

    A work unit is one selected agent execution. Limits set to zero are disabled,
    except max_work_units_per_request which is expected to be positive by Settings.
    The returned counters are projected values when allowed and current values when
    denied.
    """
    if work_units < 0:
        raise ValueError("work_units must be non-negative")
    current_time = now or datetime.now(timezone.utc)

    if (
        limits.max_work_units_per_request > 0
        and work_units > limits.max_work_units_per_request
    ):
        return ResearchUsageDecision(
            allowed=False,
            reason="request_work_unit_limit",
            work_units=work_units,
            minute_requests=counters.minute_requests,
            daily_requests=counters.daily_requests,
            daily_work_units=counters.daily_work_units,
            limit=limits.max_work_units_per_request,
        )

    if limits.requests_per_minute > 0 and counters.minute_requests + 1 > limits.requests_per_minute:
        return ResearchUsageDecision(
            allowed=False,
            reason="minute_request_limit",
            work_units=work_units,
            minute_requests=counters.minute_requests,
            daily_requests=counters.daily_requests,
            daily_work_units=counters.daily_work_units,
            limit=limits.requests_per_minute,
            retry_after_seconds=_seconds_until_next_minute(current_time),
        )

    if limits.requests_per_day > 0 and counters.daily_requests + 1 > limits.requests_per_day:
        return ResearchUsageDecision(
            allowed=False,
            reason="daily_request_quota",
            work_units=work_units,
            minute_requests=counters.minute_requests,
            daily_requests=counters.daily_requests,
            daily_work_units=counters.daily_work_units,
            limit=limits.requests_per_day,
            retry_after_seconds=_seconds_until_next_utc_day(current_time),
        )

    if limits.work_units_per_day > 0 and counters.daily_work_units + work_units > limits.work_units_per_day:
        return ResearchUsageDecision(
            allowed=False,
            reason="daily_work_unit_quota",
            work_units=work_units,
            minute_requests=counters.minute_requests,
            daily_requests=counters.daily_requests,
            daily_work_units=counters.daily_work_units,
            limit=limits.work_units_per_day,
            retry_after_seconds=_seconds_until_next_utc_day(current_time),
        )

    return ResearchUsageDecision(
        allowed=True,
        reason="allowed",
        work_units=work_units,
        minute_requests=counters.minute_requests + 1,
        daily_requests=counters.daily_requests + 1,
        daily_work_units=counters.daily_work_units + work_units,
    )
