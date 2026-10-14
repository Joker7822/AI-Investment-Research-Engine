from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import asyncpg

from app.domain.schemas import AnalysisRequest, AgentAnalysis, CIOJudgement, InvestmentReport
from app.orchestration.router import RoutePlan


@dataclass(frozen=True)
class IdempotencyReservation:
    state: Literal["reserved", "replay", "in_progress", "conflict"]
    request_id: str | None = None


class AuditRepository(ABC):
    @abstractmethod
    async def save(
        self,
        request: AnalysisRequest,
        report: InvestmentReport,
        agent_results: list[AgentAnalysis],
        route: RoutePlan,
        cio_judgement: CIOJudgement,
    ) -> None:
        raise NotImplementedError


class NullAuditRepository(AuditRepository):
    async def save(
        self,
        request: AnalysisRequest,
        report: InvestmentReport,
        agent_results: list[AgentAnalysis],
        route: RoutePlan,
        cio_judgement: CIOJudgement,
    ) -> None:
        return None

    async def get_report(self, request_id: str) -> InvestmentReport | None:
        return None

    async def reserve_idempotency(
        self, key_hash: str, request_hash: str, request_id: str
    ) -> IdempotencyReservation:
        return IdempotencyReservation("reserved", request_id)

    async def complete_idempotency(self, key_hash: str, request_hash: str) -> None:
        return None

    async def release_idempotency(self, key_hash: str, request_hash: str) -> None:
        return None


def stage_for_agent(agent_id: str, route: RoutePlan) -> str:
    if agent_id in route.specialists:
        return "specialist"
    if agent_id in route.verifiers:
        return "verification"
    if agent_id in route.critics:
        return "critic"
    if agent_id == route.cio:
        return "cio"
    return "unknown"


def _json(value) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


class PostgresAuditRepository(AuditRepository):
    """Persist immutable analysis provenance and public-API idempotency state."""

    def __init__(
        self,
        dsn: str,
        schema_path: str = "db/schema.sql",
        min_pool_size: int = 1,
        max_pool_size: int = 5,
    ):
        if not dsn.strip():
            raise ValueError("PostgreSQL DSN is required")
        self.dsn = dsn
        self.schema_path = Path(schema_path)
        self.min_pool_size = min_pool_size
        self.max_pool_size = max_pool_size
        self._pool: asyncpg.Pool | None = None
        self._pool_lock = asyncio.Lock()
        self._schema_ready = False

    async def _get_pool(self) -> asyncpg.Pool:
        if self._pool is not None:
            return self._pool
        async with self._pool_lock:
            if self._pool is None:
                self._pool = await asyncpg.create_pool(
                    dsn=self.dsn,
                    min_size=self.min_pool_size,
                    max_size=self.max_pool_size,
                )
        return self._pool

    async def _ensure_schema(self, conn: asyncpg.Connection) -> None:
        if self._schema_ready:
            return
        sql = self.schema_path.read_text(encoding="utf-8")
        await conn.execute(sql)
        self._schema_ready = True

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            self._schema_ready = False

    async def get_report(self, request_id: str) -> InvestmentReport | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await self._ensure_schema(conn)
            payload = await conn.fetchval(
                "SELECT report_json FROM reports WHERE request_id=$1",
                request_id,
            )
        if payload is None:
            return None
        if isinstance(payload, str):
            payload = json.loads(payload)
        return InvestmentReport.model_validate(payload)

    async def reserve_idempotency(
        self, key_hash: str, request_hash: str, request_id: str
    ) -> IdempotencyReservation:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await self._ensure_schema(conn)
            async with conn.transaction():
                inserted = await conn.fetchval(
                    """
                    INSERT INTO api_idempotency (
                        key_hash, request_hash, request_id, status
                    ) VALUES ($1,$2,$3,'running')
                    ON CONFLICT (key_hash) DO NOTHING
                    RETURNING key_hash
                    """,
                    key_hash,
                    request_hash,
                    request_id,
                )
                if inserted is not None:
                    return IdempotencyReservation("reserved", request_id)

                row = await conn.fetchrow(
                    """
                    SELECT request_hash, request_id, status
                    FROM api_idempotency
                    WHERE key_hash=$1
                    FOR UPDATE
                    """,
                    key_hash,
                )
                if row is None:
                    # Defensive fallback for an unexpected concurrent delete.
                    return IdempotencyReservation("in_progress", None)
                if row["request_hash"] != request_hash:
                    return IdempotencyReservation("conflict", row["request_id"])
                if row["status"] == "completed":
                    return IdempotencyReservation("replay", row["request_id"])

                report_exists = await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM reports WHERE request_id=$1)",
                    row["request_id"],
                )
                if report_exists:
                    await conn.execute(
                        """
                        UPDATE api_idempotency
                        SET status='completed', completed_at=now()
                        WHERE key_hash=$1
                        """,
                        key_hash,
                    )
                    return IdempotencyReservation("replay", row["request_id"])
                return IdempotencyReservation("in_progress", row["request_id"])

    async def complete_idempotency(self, key_hash: str, request_hash: str) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await self._ensure_schema(conn)
            await conn.execute(
                """
                UPDATE api_idempotency
                SET status='completed', completed_at=now()
                WHERE key_hash=$1 AND request_hash=$2 AND status='running'
                """,
                key_hash,
                request_hash,
            )

    async def release_idempotency(self, key_hash: str, request_hash: str) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await self._ensure_schema(conn)
            await conn.execute(
                """
                DELETE FROM api_idempotency
                WHERE key_hash=$1 AND request_hash=$2 AND status='running'
                """,
                key_hash,
                request_hash,
            )

    async def save(
        self,
        request: AnalysisRequest,
        report: InvestmentReport,
        agent_results: list[AgentAnalysis],
        route: RoutePlan,
        cio_judgement: CIOJudgement,
    ) -> None:
        if not request.request_id:
            raise ValueError("request_id must be assigned before audit persistence")

        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await self._ensure_schema(conn)
            async with conn.transaction():
                request_json = _json(request)
                await conn.execute(
                    """
                    INSERT INTO analysis_requests (
                        request_id, as_of, asset_type, ticker, exchange, currency,
                        user_question, investment_horizon, request_json
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb)
                    """,
                    request.request_id,
                    request.as_of,
                    request.asset.type,
                    request.asset.ticker,
                    request.asset.exchange,
                    request.asset.currency,
                    request.user_question,
                    request.investment_horizon,
                    request_json,
                )

                known_sources: set[str] = set()
                for source in request.evidence:
                    await conn.execute(
                        """
                        INSERT INTO sources (
                            source_id, source_type, publisher, source_url,
                            observed_at, content_hash, quality_score
                        ) VALUES ($1,$2,$3,$4,$5,$6,$7)
                        ON CONFLICT (source_id) DO NOTHING
                        """,
                        source.source_id,
                        source.source_type,
                        None,
                        source.url,
                        source.observed_at,
                        _hash({"claim": source.claim, "url": source.url}),
                        source.quality,
                    )
                    known_sources.add(source.source_id)

                request_hash = hashlib.sha256(request_json.encode("utf-8")).hexdigest()

                for result in agent_results:
                    run_id = uuid.uuid4()
                    await conn.execute(
                        """
                        INSERT INTO agent_runs (
                            run_id, request_id, agent_id, model_name, stage, status,
                            score, confidence, evidence_quality, input_hash, raw_output
                        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb)
                        """,
                        run_id,
                        request.request_id,
                        result.agent_id,
                        None,
                        stage_for_agent(result.agent_id, route),
                        result.status,
                        result.score,
                        result.confidence,
                        result.evidence_quality,
                        request_hash,
                        _json(result),
                    )

                    for fact in result.facts:
                        claim_id = uuid.uuid4()
                        await conn.execute(
                            """
                            INSERT INTO claims (
                                claim_id, request_id, agent_run_id, claim_text, claim_kind, impact
                            ) VALUES ($1,$2,$3,$4,'fact',NULL)
                            """,
                            claim_id,
                            request.request_id,
                            run_id,
                            fact.claim,
                        )
                        if fact.source_id and fact.source_id in known_sources:
                            await conn.execute(
                                """
                                INSERT INTO claim_evidence (claim_id, source_id, support_strength)
                                VALUES ($1,$2,1.0)
                                """,
                                claim_id,
                                fact.source_id,
                            )

                    for item in result.analysis:
                        await conn.execute(
                            """
                            INSERT INTO claims (
                                claim_id, request_id, agent_run_id, claim_text, claim_kind, impact
                            ) VALUES ($1,$2,$3,$4,'analysis',$5)
                            """,
                            uuid.uuid4(),
                            request.request_id,
                            run_id,
                            item.claim,
                            item.impact,
                        )

                    for risk in result.risks:
                        await conn.execute(
                            """
                            INSERT INTO claims (
                                claim_id, request_id, agent_run_id, claim_text, claim_kind, impact
                            ) VALUES ($1,$2,$3,$4,'risk',NULL)
                            """,
                            uuid.uuid4(),
                            request.request_id,
                            run_id,
                            risk,
                        )

                    for invalidator in result.invalidators:
                        await conn.execute(
                            """
                            INSERT INTO claims (
                                claim_id, request_id, agent_run_id, claim_text, claim_kind, impact
                            ) VALUES ($1,$2,$3,$4,'invalidator',NULL)
                            """,
                            uuid.uuid4(),
                            request.request_id,
                            run_id,
                            invalidator,
                        )

                await conn.execute(
                    """
                    INSERT INTO agent_runs (
                        run_id, request_id, agent_id, model_name, stage, status,
                        score, confidence, evidence_quality, input_hash, raw_output
                    ) VALUES ($1,$2,'A100',NULL,'cio','completed',$3,$4,$5,$6,$7::jsonb)
                    """,
                    uuid.uuid4(),
                    request.request_id,
                    report.score,
                    report.confidence,
                    report.evidence_quality,
                    request_hash,
                    _json(cio_judgement),
                )

                await conn.execute(
                    """
                    INSERT INTO reports (
                        request_id, stance, score, confidence, evidence_quality, report_json
                    ) VALUES ($1,$2,$3,$4,$5,$6::jsonb)
                    """,
                    request.request_id,
                    report.stance.value,
                    report.score,
                    report.confidence,
                    report.evidence_quality,
                    _json(report),
                )
