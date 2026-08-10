import asyncio
from app.activities.openclaw_activities import send_to_openclaw

async def main():
    try:
        res = await send_to_openclaw({"message": "test plan payload", "session_key": "agent:main:main"})
        print(res)
    except Exception as e:
        print("Error:", e)

asyncio.run(main())
