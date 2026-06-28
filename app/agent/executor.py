"""Safe execution wrapper for compiled agent queries."""
from __future__ import annotations

import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.schemas import CompiledQuery
from app.core.config import get_settings

settings = get_settings()


class AgentExecutor:
    """Execute compiled queries under read-only and timeout guards."""

    async def execute(self, db: AsyncSession, compiled: CompiledQuery) -> tuple[list[str], list[list[Any]], float]:
        if " limit " not in compiled.sql.lower():
            raise ValueError("Agent queries must include an explicit LIMIT.")
        if compiled.limit > settings.AGENT_MAX_LIMIT:
            raise ValueError("Agent query exceeds the configured row limit.")
        if compiled.join_count > settings.AGENT_MAX_JOINS:
            raise ValueError("Agent query exceeds the configured join limit.")

        async with db.begin():
            await db.execute(text("SET TRANSACTION READ ONLY"))
            await db.execute(text(f"SET LOCAL statement_timeout = {int(settings.AGENT_STATEMENT_TIMEOUT_MS)}"))

            started = time.perf_counter()
            result = await db.execute(text(compiled.sql), compiled.params)
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)

            columns = list(result.keys())
            rows = [list(row) for row in result.fetchmany(int(settings.AGENT_MAX_ROWS))]
            return columns, rows, elapsed_ms

    async def execute_batch(
        self,
        db: AsyncSession,
        queries: list[CompiledQuery],
    ) -> list[tuple[list[str], list[list[Any]], float]]:
        """Execute multiple compiled queries sequentially in a read-only transaction."""
        results: list[tuple[list[str], list[list[Any]], float]] = []
        async with db.begin():
            await db.execute(text("SET TRANSACTION READ ONLY"))
            await db.execute(text(f"SET LOCAL statement_timeout = {int(settings.AGENT_STATEMENT_TIMEOUT_MS)}"))

            for compiled in queries:
                if " limit " not in compiled.sql.lower():
                    raise ValueError("Agent queries must include an explicit LIMIT.")
                if compiled.limit > settings.AGENT_MAX_LIMIT:
                    raise ValueError("Agent query exceeds the configured row limit.")
                if compiled.join_count > settings.AGENT_MAX_JOINS:
                    raise ValueError("Agent query exceeds the configured join limit.")

                started = time.perf_counter()
                result = await db.execute(text(compiled.sql), compiled.params)
                elapsed_ms = round((time.perf_counter() - started) * 1000, 2)

                columns = list(result.keys())
                rows = [list(row) for row in result.fetchmany(int(settings.AGENT_MAX_ROWS))]
                results.append((columns, rows, elapsed_ms))

        return results
