import asyncio

from config import config
from storage.db import Database


async def main():
    db = Database(config.DB_PATH)
    await db.initialize()
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
