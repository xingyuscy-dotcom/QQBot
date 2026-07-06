import asyncio
import json

import websockets


async def main() -> None:
    async with websockets.connect("ws://127.0.0.1:6199/onebot/ws") as ws:
        event = {
            "post_type": "message",
            "message_type": "group",
            "self_id": 123456,
            "group_id": 987654,
            "user_id": 111222,
            "message_id": 1,
            "message": [{"type": "text", "data": {"text": "/ping"}}],
            "raw_message": "/ping",
        }
        await ws.send(json.dumps(event, ensure_ascii=False))
        reply = await asyncio.wait_for(ws.recv(), timeout=3)
        print(reply)


if __name__ == "__main__":
    asyncio.run(main())
