from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import asyncpg

from app.metrics.edinet_silver import SilverMetric
from app.providers.edinet_csv import ParsedEdinetCsvPackage


class EdinetIndexRepository:
    def __init__(self, dsn: str, schema_path: str = "db/schema.sql", min_pool_size: int = 1, max_pool_size: int = 5):
        if not dsn.strip():
            raise ValueError("PostgreSQL DSN is required")
        self.dsn = dsn
        self.schema_path = Path(schema_path)
        self.min_pool_size = min_pool_size
        self.max_pool_size = max_pool_size
        self._pool: asyncpg.Pool | None = None
        self._pool_lock = asyncio.Lock()
        self._schema_lock = asyncio.Lock()
        self._schema_ready = False

    async def _get_pool(self) -> asyncpg.Pool:
        if self._pool is not None:
            return self._pool
        async with self._pool_lock:
            if self._pool is None:
                self._pool = await asyncpg.create_pool(dsn=self.dsn, min_size=self.min_pool_size, max_size=self.max_pool_size)
        return self._pool

    async def _ensure_schema(self, conn: asyncpg.Connection) -> None:
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            await conn.execute(self.schema_path.read_text(encoding="utf-8"))
            self._schema_ready = True

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            self._schema_ready = False

    async def start_sync(self, target_date: date) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await self._ensure_schema(conn)
            await conn.execute("""
                INSERT INTO edinet_sync_runs (sync_date, status, document_count, started_at, completed_at, error_text)
                VALUES ($1, 'running', NULL, now(), NULL, NULL)
                ON CONFLICT (sync_date) DO UPDATE SET
                    status='running', document_count=NULL, started_at=now(), completed_at=NULL, error_text=NULL
            """, target_date)

    async def upsert_filings(self, target_date: date, rows: list[dict[str, Any]]) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await self._ensure_schema(conn)
            async with conn.transaction():
                for row in rows:
                    await conn.execute("""
                        INSERT INTO edinet_filings (
                            doc_id, filing_date, submitted_at, edinet_code, sec_code, jcn,
                            filer_name, ordinance_code, form_code, doc_type_code,
                            period_start, period_end, xbrl_flag, csv_flag, pdf_flag, legal_status, raw_json
                        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17::jsonb)
                        ON CONFLICT (doc_id) DO UPDATE SET
                            filing_date=EXCLUDED.filing_date, submitted_at=EXCLUDED.submitted_at,
                            edinet_code=EXCLUDED.edinet_code, sec_code=EXCLUDED.sec_code, jcn=EXCLUDED.jcn,
                            filer_name=EXCLUDED.filer_name, ordinance_code=EXCLUDED.ordinance_code,
                            form_code=EXCLUDED.form_code, doc_type_code=EXCLUDED.doc_type_code,
                            period_start=EXCLUDED.period_start, period_end=EXCLUDED.period_end,
                            xbrl_flag=EXCLUDED.xbrl_flag, csv_flag=EXCLUDED.csv_flag,
                            pdf_flag=EXCLUDED.pdf_flag, legal_status=EXCLUDED.legal_status,
                            raw_json=EXCLUDED.raw_json, indexed_at=now()
                    """,
                    row["doc_id"], target_date, row.get("submitted_at"), row.get("edinet_code"), row.get("sec_code"),
                    row.get("jcn"), row.get("filer_name"), row.get("ordinance_code"), row.get("form_code"),
                    row.get("doc_type_code"), row.get("period_start"), row.get("period_end"), bool(row.get("xbrl_flag")),
                    bool(row.get("csv_flag")), bool(row.get("pdf_flag")), row.get("legal_status"),
                    json.dumps(row.get("raw_json", {}), ensure_ascii=False))

    async def complete_sync(self, target_date: date, document_count: int) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await self._ensure_schema(conn)
            await conn.execute("UPDATE edinet_sync_runs SET status='completed', document_count=$2, completed_at=now(), error_text=NULL WHERE sync_date=$1", target_date, document_count)

    async def fail_sync(self, target_date: date, error_text: str) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await self._ensure_schema(conn)
            await conn.execute("""
                INSERT INTO edinet_sync_runs (sync_date, status, document_count, completed_at, error_text)
                VALUES ($1, 'failed', NULL, now(), $2)
                ON CONFLICT (sync_date) DO UPDATE SET status='failed', completed_at=now(), error_text=EXCLUDED.error_text
            """, target_date, error_text[:4000])

    async def latest_for_sec_code(self, sec_code: str, as_of: datetime, limit: int) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await self._ensure_schema(conn)
            rows = await conn.fetch("""
                SELECT doc_id, filing_date, submitted_at, edinet_code, sec_code, jcn,
                       filer_name, ordinance_code, form_code, doc_type_code,
                       period_start, period_end, xbrl_flag, csv_flag, pdf_flag, legal_status
                FROM edinet_filings
                WHERE sec_code=$1 AND submitted_at IS NOT NULL AND submitted_at <= $2
                ORDER BY submitted_at DESC, doc_id DESC LIMIT $3
            """, sec_code, as_of, limit)
        return [dict(row) for row in rows]

    async def get_csv_eligible_filing(self, doc_id: str) -> dict[str, Any] | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await self._ensure_schema(conn)
            row = await conn.fetchrow("""
                SELECT doc_id, submitted_at, sec_code, filer_name, doc_type_code, csv_flag, legal_status
                FROM edinet_filings WHERE doc_id=$1
            """, doc_id)
        return dict(row) if row else None

    @staticmethod
    async def _replace_bronze(
        conn: asyncpg.Connection,
        doc_id: str,
        package: ParsedEdinetCsvPackage,
    ) -> bool:
        existing = await conn.fetchval(
            "SELECT package_sha256 FROM edinet_csv_packages WHERE doc_id=$1 FOR UPDATE",
            doc_id,
        )
        if existing == package.sha256:
            return False

        await conn.execute("DELETE FROM edinet_csv_facts WHERE doc_id=$1", doc_id)
        records = [(
            doc_id, fact.source_file, fact.row_number, fact.element_id, fact.item_name,
            fact.context_id, fact.relative_year, fact.consolidation, fact.period_type,
            fact.unit_id, fact.unit_label, fact.value,
        ) for fact in package.facts]
        if records:
            await conn.copy_records_to_table("edinet_csv_facts", records=records, columns=(
                "doc_id", "source_file", "row_number", "element_id", "item_name", "context_id",
                "relative_year", "consolidation", "period_type", "unit_id", "unit_label", "raw_value"))
        await conn.execute("""
            INSERT INTO edinet_csv_packages (doc_id, package_sha256, source_file_count, fact_count, fetched_at)
            VALUES ($1,$2,$3,$4,now())
            ON CONFLICT (doc_id) DO UPDATE SET package_sha256=EXCLUDED.package_sha256,
                source_file_count=EXCLUDED.source_file_count, fact_count=EXCLUDED.fact_count, fetched_at=now()
        """, doc_id, package.sha256, len(package.files), len(package.facts))
        return True

    @staticmethod
    async def _replace_silver(
        conn: asyncpg.Connection,
        doc_id: str,
        metrics: list[SilverMetric],
    ) -> None:
        await conn.execute("DELETE FROM edinet_silver_metrics WHERE doc_id=$1", doc_id)
        records = [(
            doc_id, metric.metric_name, metric.accounting_standard, metric.mapping_version,
            metric.mapping_priority, metric.scope, metric.period_key, metric.element_id,
            metric.item_name, metric.context_id, metric.relative_year, metric.period_type,
            metric.unit_id, metric.unit_label, metric.raw_value, metric.numeric_value,
            metric.source_file, metric.row_number,
        ) for metric in metrics]
        if records:
            await conn.copy_records_to_table("edinet_silver_metrics", records=records, columns=(
                "doc_id", "metric_name", "accounting_standard", "mapping_version", "mapping_priority",
                "scope", "period_key", "element_id", "item_name", "context_id", "relative_year",
                "period_type", "unit_id", "unit_label", "raw_value", "numeric_value",
                "source_file", "row_number"))

    async def store_csv_and_silver(
        self,
        doc_id: str,
        package: ParsedEdinetCsvPackage,
        metrics: list[SilverMetric],
    ) -> bool:
        """Atomically update Bronze and rebuild Silver; return whether Bronze changed.

        Silver is rebuilt even when the ZIP hash is unchanged so a versioned mapping
        change can be applied without forcing a source re-download/rewrite.
        """
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await self._ensure_schema(conn)
            async with conn.transaction():
                bronze_changed = await self._replace_bronze(conn, doc_id, package)
                await self._replace_silver(conn, doc_id, metrics)
        return bronze_changed

    async def store_csv_package(self, doc_id: str, package: ParsedEdinetCsvPackage) -> bool:
        """Backward-compatible Bronze-only write path."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await self._ensure_schema(conn)
            async with conn.transaction():
                return await self._replace_bronze(conn, doc_id, package)

    async def store_silver_metrics(self, doc_id: str, metrics: list[SilverMetric]) -> None:
        """Backward-compatible Silver-only rebuild path."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await self._ensure_schema(conn)
            async with conn.transaction():
                await self._replace_silver(conn, doc_id, metrics)

    async def silver_metrics_for_docs(self, doc_ids: list[str]) -> list[dict[str, Any]]:
        if not doc_ids:
            return []
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await self._ensure_schema(conn)
            rows = await conn.fetch("""
                SELECT doc_id, metric_name, accounting_standard, mapping_version, mapping_priority,
                       scope, period_key, element_id, item_name, context_id, relative_year,
                       period_type, unit_id, unit_label, raw_value, numeric_value,
                       source_file, row_number
                FROM edinet_silver_metrics
                WHERE doc_id = ANY($1::text[])
            """, doc_ids)
        order = {doc_id: idx for idx, doc_id in enumerate(doc_ids)}
        result = [dict(row) for row in rows]
        result.sort(key=lambda row: (
            order.get(row["doc_id"], 10_000), row["metric_name"],
            0 if row["period_key"] == "CurrentYear" else 1,
            0 if row["scope"] == "consolidated" else 1,
            row["mapping_priority"], row["context_id"],
        ))
        return result

    async def csv_fact_summary(self, doc_id: str) -> dict[str, Any] | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await self._ensure_schema(conn)
            row = await conn.fetchrow(
                "SELECT doc_id, package_sha256, source_file_count, fact_count, fetched_at FROM edinet_csv_packages WHERE doc_id=$1",
                doc_id,
            )
        return dict(row) if row else None
