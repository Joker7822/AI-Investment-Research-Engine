from __future__ import annotations

import asyncio
from pathlib import Path

import asyncpg

from app.core.settings import get_settings


async def migrate() -> None:
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required for database migration")

    schema_path = Path(settings.database_schema_path)
    if not schema_path.exists():
        raise RuntimeError(f"Database schema file not found: {schema_path}")

    sql = schema_path.read_text(encoding="utf-8")
    conn = await asyncpg.connect(settings.database_url)
    try:
        async with conn.transaction():
            await conn.execute(sql)
    finally:
        await conn.close()


def main() -> None:
    asyncio.run(migrate())


if __name__ == "__main__":
    main()
