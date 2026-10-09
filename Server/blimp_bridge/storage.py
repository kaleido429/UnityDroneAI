import asyncio
import json
import sqlite3
from contextlib import closing


class Storage:
    def __init__(self, path):
        self.path = str(path)
        self.lock = asyncio.Lock()

    def _execute(self, sql, params=(), fetch=False):
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                rows = db.execute(sql, params)
                return rows.fetchall() if fetch else None

    async def execute(self, sql, params=(), fetch=False):
        async with self.lock:
            return await asyncio.to_thread(self._execute, sql, params, fetch)

    async def init(self):
        await self.execute("CREATE TABLE IF NOT EXISTS registry (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        await self.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL)")

    async def registry(self):
        return [json.loads(row[0]) for row in await self.execute("SELECT body FROM registry", fetch=True)]

    async def enroll(self, drone):
        await self.execute("INSERT INTO registry VALUES (?, ?)", (drone.drone_id, drone.model_dump_json()))

    async def event(self, event):
        await self.execute("INSERT INTO events(body) VALUES (?)", (json.dumps(event),))
