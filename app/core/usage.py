from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
    next_minute = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    return max(1, math.ceil((next_minute - now).total_seconds()))


def _seconds_until_next_utc_day(now: datetime) -> int:
    utc_now = now.astimezone(timezone.utc)
    next_day = utc_now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return max(1, math.ceil((next_day - utc_now).total_seconds()))


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
    denied. When multiple time-window quotas are exhausted, Retry-After uses the
    longest active reset delay so a conforming client does not retry too early.
    """
    if work_units < 0:
        raise ValueError("work_units must be non-negative")
    current_time = now or datetime.now(timezone.utc)

    intrinsic_limits = [limits.max_work_units_per_request]
    if limits.work_units_per_day > 0:
        intrinsic_limits.append(limits.work_units_per_day)
    intrinsic_work_unit_limit = min(intrinsic_limits)
    if work_units > intrinsic_work_unit_limit:
        return ResearchUsageDecision(
            allowed=False,
            reason="request_work_unit_limit",
            work_units=work_units,
            minute_requests=counters.minute_requests,
            daily_requests=counters.daily_requests,
            daily_work_units=counters.daily_work_units,
            limit=intrinsic_work_unit_limit,
        )

    violations: list[tuple[int, UsageLimitReason, int]] = []
    if (
        limits.requests_per_minute > 0
        and counters.minute_requests + 1 > limits.requests_per_minute
    ):
        violations.append(
            (
                _seconds_until_next_minute(current_time),
                "minute_request_limit",
                limits.requests_per_minute,
            )
        )

    if limits.requests_per_day > 0 and counters.daily_requests + 1 > limits.requests_per_day:
        violations.append(
            (
                _seconds_until_next_utc_day(current_time),
                "daily_request_quota",
                limits.requests_per_day,
            )
        )

    if (
        limits.work_units_per_day > 0
        and counters.daily_work_units + work_units > limits.work_units_per_day
    ):
        violations.append(
            (
                _seconds_until_next_utc_day(current_time),
                "daily_work_unit_quota",
                limits.work_units_per_day,
            )
        )

    if violations:
        retry_after_seconds, reason, limit = max(violations, key=lambda item: item[0])
        return ResearchUsageDecision(
            allowed=False,
            reason=reason,
            work_units=work_units,
            minute_requests=counters.minute_requests,
            daily_requests=counters.daily_requests,
            daily_work_units=counters.daily_work_units,
            limit=limit,
            retry_after_seconds=retry_after_seconds,
        )

    return ResearchUsageDecision(
        allowed=True,
        reason="allowed",
        work_units=work_units,
        minute_requests=counters.minute_requests + 1,
        daily_requests=counters.daily_requests + 1,
        daily_work_units=counters.daily_work_units + work_units,
    )
