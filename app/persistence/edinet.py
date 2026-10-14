from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import asyncpg


class EdinetIndexRepository:
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
        self._schema_lock = asyncio.Lock()
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
            await conn.execute(
                """
                INSERT INTO edinet_sync_runs (sync_date, status, document_count, started_at, completed_at, error_text)
                VALUES ($1, 'running', NULL, now(), NULL, NULL)
                ON CONFLICT (sync_date) DO UPDATE SET
                    status='running', document_count=NULL, started_at=now(), completed_at=NULL, error_text=NULL
                """,
                target_date,
            )

    async def upsert_filings(self, target_date: date, rows: list[dict[str, Any]]) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await self._ensure_schema(conn)
            async with conn.transaction():
                for row in rows:
                    await conn.execute(
                        """
                        INSERT INTO edinet_filings (
                            doc_id, filing_date, submitted_at, edinet_code, sec_code, jcn,
                            filer_name, ordinance_code, form_code, doc_type_code,
                            period_start, period_end, xbrl_flag, csv_flag, pdf_flag,
                            legal_status, raw_json
                        ) VALUES (
                            $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17::jsonb
                        )
                        ON CONFLICT (doc_id) DO UPDATE SET
                            filing_date=EXCLUDED.filing_date,
                            submitted_at=EXCLUDED.submitted_at,
                            edinet_code=EXCLUDED.edinet_code,
                            sec_code=EXCLUDED.sec_code,
                            jcn=EXCLUDED.jcn,
                            filer_name=EXCLUDED.filer_name,
                            ordinance_code=EXCLUDED.ordinance_code,
                            form_code=EXCLUDED.form_code,
                            doc_type_code=EXCLUDED.doc_type_code,
                            period_start=EXCLUDED.period_start,
                            period_end=EXCLUDED.period_end,
                            xbrl_flag=EXCLUDED.xbrl_flag,
                            csv_flag=EXCLUDED.csv_flag,
                            pdf_flag=EXCLUDED.pdf_flag,
                            legal_status=EXCLUDED.legal_status,
                            raw_json=EXCLUDED.raw_json,
                            indexed_at=now()
                        """,
                        row["doc_id"],
                        target_date,
                        row.get("submitted_at"),
                        row.get("edinet_code"),
                        row.get("sec_code"),
                        row.get("jcn"),
                        row.get("filer_name"),
                        row.get("ordinance_code"),
                        row.get("form_code"),
                        row.get("doc_type_code"),
                        row.get("period_start"),
                        row.get("period_end"),
                        bool(row.get("xbrl_flag")),
                        bool(row.get("csv_flag")),
                        bool(row.get("pdf_flag")),
                        row.get("legal_status"),
                        json.dumps(row.get("raw_json", {}), ensure_ascii=False),
                    )

    async def complete_sync(self, target_date: date, document_count: int) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await self._ensure_schema(conn)
            await conn.execute(
                """
                UPDATE edinet_sync_runs
                SET status='completed', document_count=$2, completed_at=now(), error_text=NULL
                WHERE sync_date=$1
                """,
                target_date,
                document_count,
            )

    async def fail_sync(self, target_date: date, error_text: str) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await self._ensure_schema(conn)
            await conn.execute(
                """
                INSERT INTO edinet_sync_runs (sync_date, status, document_count, completed_at, error_text)
                VALUES ($1, 'failed', NULL, now(), $2)
                ON CONFLICT (sync_date) DO UPDATE SET
                    status='failed', completed_at=now(), error_text=EXCLUDED.error_text
                """,
                target_date,
                error_text[:4000],
            )

    async def latest_for_sec_code(
        self,
        sec_code: str,
        as_of: datetime,
        limit: int,
    ) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await self._ensure_schema(conn)
            rows = await conn.fetch(
                """
                SELECT doc_id, filing_date, submitted_at, edinet_code, sec_code, jcn,
                       filer_name, ordinance_code, form_code, doc_type_code,
                       period_start, period_end, xbrl_flag, csv_flag, pdf_flag,
                       legal_status
                FROM edinet_filings
                WHERE sec_code=$1
                  AND submitted_at IS NOT NULL
                  AND submitted_at <= $2
                ORDER BY submitted_at DESC, doc_id DESC
                LIMIT $3
                """,
                sec_code,
                as_of,
                limit,
            )
        return [dict(row) for row in rows]
