import asyncio
from sqlalchemy import select
from app.db.database import AsyncSessionLocal
from app.db.models import Task

async def main():
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Task).order_by(Task.created_at.desc()).limit(5))
        tasks = result.scalars().all()
        for t in tasks:
            print(f"ID: {t.id}, Status: {t.status}")

asyncio.run(main())
