import asyncio

from .header import OFPT_HELLO, Message, pack_message
from .reader import MessageReader


async def hello_handshake(host: str, port: int, xid: int = 0) -> Message:
    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(pack_message(Message(OFPT_HELLO, xid=xid)))
        await writer.drain()

        message_reader = MessageReader()
        while True:
            data = await reader.read(4096)
            if data == b"":
                raise ConnectionError("Controller closed before sending HELLO")

            messages = message_reader.feed(data)
            if messages:
                reply = messages[0]
                if reply.type != OFPT_HELLO:
                    raise ValueError("First controller message must be HELLO")
                return reply
    finally:
        writer.close()
        await writer.wait_closed()