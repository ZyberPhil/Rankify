from __future__ import annotations

import asyncio
from collections.abc import Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite


class DatabaseManager:
    def __init__(self, database_path: Path, schema_path: Path, migrations_path: Path) -> None:
        self.database_path = database_path
        self.schema_path = schema_path
        self.migrations_path = migrations_path
        self._write_lock = asyncio.Lock()
        self._conn: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        schema_sql = self.schema_path.read_text(encoding="utf-8")
        # keep a persistent connection for the lifetime of the manager
        if self._conn is None:
            self._conn = await aiosqlite.connect(self.database_path)
        db = self._conn
        await db.execute("PRAGMA foreign_keys = ON;")
        db.row_factory = aiosqlite.Row
        await db.executescript(schema_sql)
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await self._ensure_user_booster_columns(db)
        await self._ensure_team_columns(db)
        await self._ensure_order_deadline_columns(db)
        await self._apply_pending_migrations(db)
        await db.commit()

    async def _ensure_user_booster_columns(self, db: aiosqlite.Connection) -> None:
        async with db.execute("PRAGMA table_info(users)") as cursor:
            existing_columns = {row[1] for row in await cursor.fetchall()}

        required_columns = {
            "boosted_since": "TEXT",
            "verified_booster": "INTEGER NOT NULL DEFAULT 0 CHECK (verified_booster IN (0, 1))",
            "rules_confirmed_at": "TEXT",
        }

        for column_name, column_sql in required_columns.items():
            if column_name in existing_columns:
                continue
            await db.execute(f"ALTER TABLE users ADD COLUMN {column_name} {column_sql}")

    async def _ensure_team_columns(self, db: aiosqlite.Connection) -> None:
        async with db.execute("PRAGMA table_info(teams)") as cursor:
            existing_columns = {row[1] for row in await cursor.fetchall()}

        for column_name, column_sql in {
            "prefix": "TEXT NOT NULL DEFAULT ''",
            "referral_code": "TEXT",
        }.items():
            if column_name in existing_columns:
                continue
            await db.execute(f"ALTER TABLE teams ADD COLUMN {column_name} {column_sql}")

    async def _ensure_order_deadline_columns(self, db: aiosqlite.Connection) -> None:
        async with db.execute("PRAGMA table_info(orders)") as cursor:
            existing_columns = {row[1] for row in await cursor.fetchall()}

        for column_name, column_sql in {
            "claim_deadline_hours": "INTEGER",
            "deadline_reminder_sent_at": "TEXT",
            "note": "TEXT",
            "account_username": "TEXT",
            "account_password": "TEXT",
        }.items():
            if column_name in existing_columns:
                continue
            await db.execute(f"ALTER TABLE orders ADD COLUMN {column_name} {column_sql}")

    async def _apply_pending_migrations(self, db: aiosqlite.Connection) -> None:
        if not self.migrations_path.exists():
            return
        migration_files = sorted(self.migrations_path.glob("*.sql"))
        for migration_file in migration_files:
            migration_name = migration_file.name
            async with db.execute(
                "SELECT 1 FROM schema_migrations WHERE name = ?",
                (migration_name,),
            ) as cursor:
                exists = await cursor.fetchone()
            if exists is not None:
                continue
            migration_sql = migration_file.read_text(encoding="utf-8")
            await db.executescript(migration_sql)
            await db.execute(
                "INSERT INTO schema_migrations (name) VALUES (?)",
                (migration_name,),
            )

    async def connect(self) -> aiosqlite.Connection:
        # return the persistent connection (create if missing)
        if self._conn is None:
            self._conn = await aiosqlite.connect(self.database_path)
            await self._conn.execute("PRAGMA foreign_keys = ON;")
            self._conn.row_factory = aiosqlite.Row
        return self._conn

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[aiosqlite.Connection]:
        db = await self.connect()
        try:
            yield db
        finally:
            # persistent connection: do not close here
            pass

    async def close(self) -> None:
        """Close the persistent connection (call on shutdown)."""
        if self._conn is not None:
            try:
                await self._conn.close()
            finally:
                self._conn = None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._write_lock, self.connection() as db:
            try:
                await db.execute("BEGIN")
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def fetchone(self, query: str, params: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        async with self.connection() as db:
            async with db.execute(query, params) as cursor:
                return await cursor.fetchone()

    async def fetchall(self, query: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        async with self.connection() as db:
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                return list(rows)

    async def execute(self, query: str, params: tuple[Any, ...] = ()) -> int:
        async with self._write_lock, self.connection() as db:
            cursor = await db.execute(query, params)
            await db.commit()
            return cursor.lastrowid

    async def executemany(
        self, query: str, params: Iterable[tuple[Any, ...]]
    ) -> None:
        async with self._write_lock, self.connection() as db:
            await db.executemany(query, params)
            await db.commit()
