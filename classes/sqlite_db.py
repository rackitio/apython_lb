import json
import logging
import os
from datetime import datetime

import aiosqlite

logger = logging.getLogger(__name__)


class AsyncDb:
    def __init__(self, db_path=os.environ["SQLITE_DB"]):
        self.db_uri = db_path
        self.db = None

    async def initialize(self):
        if self.db is None:
            self.db = await aiosqlite.connect(self.db_uri)
            self.db.row_factory = aiosqlite.Row
            await self.db.execute("PRAGMA journal_mode=WAL")
            await self.db.execute("PRAGMA synchronous=NORMAL")
            await self.init_db()

    async def init_db(self):
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS MyTable (
                Id INTEGER PRIMARY KEY,
                Name TEXT,
                Data TEXT,
                LastSelected TIMESTAMP
            )
        """)
        await self.db.commit()

    async def _ensure_initialized(self):
        """Ensure database is initialized"""
        if self.db is None:
            await self.initialize()

    async def insert_config(self, name, config_data):
        """Insert a configuration entry"""
        await self._ensure_initialized()
        await self.db.execute(
            "INSERT INTO MyTable (Name, Data, LastSelected) VALUES (?, ?, ?)",
            (name, json.dumps(config_data), datetime.now()),
        )
        await self.db.commit()

    async def get_all_configs(self):
        """Get all configurations"""
        await self._ensure_initialized()
        cursor = await self.db.execute(
            "SELECT Id, Name, Data, LastSelected FROM MyTable ORDER BY LastSelected DESC"
        )
        rows = await cursor.fetchall()

        configs = []
        for row in rows:
            configs.append(
                {
                    "id": row["Id"],
                    "name": row["Name"],
                    "data": json.loads(row["Data"]),
                    "last_selected": row["LastSelected"],
                }
            )
        return configs

    async def get_config_by_name(self, name):
        """Get a specific configuration by name"""
        await self._ensure_initialized()
        cursor = await self.db.execute(
            "SELECT Id, Name, Data, LastSelected FROM MyTable WHERE Name = ?", (name,)
        )
        row = await cursor.fetchone()
        if row:
            return {
                "id": row["Id"],
                "name": row["Name"],
                "data": json.loads(row["Data"]),
                "last_selected": row["LastSelected"],
            }
        return None

    async def update_last_selected(self, config_id):
        """Update the last selected timestamp"""
        await self._ensure_initialized()
        await self.db.execute(
            "UPDATE MyTable SET LastSelected = ? WHERE Id = ?",
            (datetime.now(), config_id),
        )
        await self.db.commit()

    async def delete_config(self, config_id: int):
        """Delete a configuration entry by ID"""
        await self._ensure_initialized()
        await self.db.execute("DELETE FROM MyTable WHERE Id = ?", (config_id,))
        await self.db.commit()
