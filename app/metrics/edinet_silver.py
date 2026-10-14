from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable

import yaml

from app.providers.edinet_csv import EdinetCsvFact


ANNUAL_CONTEXT_RE = re.compile(
    r"^(?P<period>CurrentYear|Prior[1-9]Year)(?P<kind>Duration|Instant)"
    r"(?P<nonconsolidated>_NonConsolidatedMember)?$"
)


@dataclass(frozen=True)
class MetricConfig:
    name: str
    period: str
    element_ids: tuple[str, ...]


@dataclass(frozen=True)
class SilverMetric:
    metric_name: str
    accounting_standard: str
    mapping_version: str
    mapping_priority: int
    scope: str
    period_key: str
    element_id: str
    item_name: str
    context_id: str
    relative_year: str
    period_type: str
    unit_id: str
    unit_label: str
    raw_value: str
    numeric_value: Decimal | None
    source_file: str
    row_number: int


def load_metric_map(path: str | Path) -> tuple[str, dict[str, dict[str, MetricConfig]]]:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    version = str(payload["version"])
    by_standard: dict[str, dict[str, MetricConfig]] = {
        "jp_gaap": {},
        "ifrs": {},
        "us_gaap": {},
    }
    for metric_name, definition in payload["metrics"].items():
        period = str(definition["period"])
        if period not in {"duration", "instant"}:
            raise ValueError(f"Invalid period for {metric_name}: {period}")
        for standard in by_standard:
            by_standard[standard][metric_name] = MetricConfig(
                name=metric_name,
                period=period,
                element_ids=tuple(definition.get(standard, []) or []),
            )
    return version, by_standard


def _annual_context(fact: EdinetCsvFact) -> tuple[str, str] | None:
    match = ANNUAL_CONTEXT_RE.match(fact.context_id)
    if match is None:
        return None
    kind = "duration" if match.group("kind") == "Duration" else "instant"
    scope = "individual" if match.group("nonconsolidated") else "consolidated"
    return match.group("period"), f"{kind}:{scope}"


def detect_accounting_standard(facts: Iterable[EdinetCsvFact]) -> str:
    current_ids = [
        fact.element_id
        for fact in facts
        if fact.context_id.startswith("CurrentYear")
        and ANNUAL_CONTEXT_RE.match(fact.context_id)
    ]
    if any("USGAAP" in element_id for element_id in current_ids):
        return "us_gaap"
    if any("IFRS" in element_id or element_id.startswith("ifrs-full:") for element_id in current_ids):
        return "ifrs"
    return "jp_gaap"


def parse_numeric(raw: str) -> Decimal | None:
    value = raw.strip().replace(",", "")
    if not value or value in {"-", "—", "―", "－"}:
        return None
    negative = False
    if value.startswith(("△", "▲")):
        negative = True
        value = value[1:].strip()
    if value.startswith("(") and value.endswith(")"):
        negative = True
        value = value[1:-1].strip()
    if value.endswith("%") or value.endswith("％"):
        value = value[:-1].strip()
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    return -number if negative else number


def extract_silver_metrics(
    facts: Iterable[EdinetCsvFact],
    *,
    config_path: str | Path = "config/edinet_metrics.yaml",
) -> tuple[str, str, list[SilverMetric]]:
    facts = tuple(facts)
    mapping_version, mappings = load_metric_map(config_path)
    standard = detect_accounting_standard(facts)
    metric_configs = mappings[standard]

    reverse_map: dict[str, list[tuple[str, str, int]]] = {}
    for metric_name, config in metric_configs.items():
        for priority, element_id in enumerate(config.element_ids):
            reverse_map.setdefault(element_id, []).append((metric_name, config.period, priority))

    results: list[SilverMetric] = []
    seen: set[tuple[str, str, str, str]] = set()
    for fact in facts:
        matches = reverse_map.get(fact.element_id)
        if not matches:
            continue
        context = _annual_context(fact)
        if context is None:
            continue
        period_key, kind_scope = context
        kind, scope = kind_scope.split(":", 1)
        for metric_name, expected_period, priority in matches:
            if kind != expected_period:
                continue
            key = (metric_name, fact.context_id, fact.element_id, fact.unit_id)
            if key in seen:
                continue
            seen.add(key)
            results.append(
                SilverMetric(
                    metric_name=metric_name,
                    accounting_standard=standard,
                    mapping_version=mapping_version,
                    mapping_priority=priority,
                    scope=scope,
                    period_key=period_key,
                    element_id=fact.element_id,
                    item_name=fact.item_name,
                    context_id=fact.context_id,
                    relative_year=fact.relative_year,
                    period_type=fact.period_type,
                    unit_id=fact.unit_id,
                    unit_label=fact.unit_label,
                    raw_value=fact.value,
                    numeric_value=parse_numeric(fact.value),
                    source_file=fact.source_file,
                    row_number=fact.row_number,
                )
            )

    results.sort(
        key=lambda item: (
            item.metric_name,
            0 if item.period_key == "CurrentYear" else 1,
            0 if item.scope == "consolidated" else 1,
            item.mapping_priority,
            item.context_id,
        )
    )
    return mapping_version, standard, results
