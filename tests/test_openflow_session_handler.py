import asyncio

from sdnsim.openflow.header import (
    Message, OFPT_ECHO_REPLY, OFPT_ECHO_REQUEST, OFPT_HELLO, pack_message,
)
from sdnsim.openflow.reader import MessageReader
from sdnsim.openflow.session import OpenFlowSession

UNKNOWN = Message(99, xid=5, body=b"x")


async def _run(on_message):
    echo_reply = []

    async def handler(reader, writer):
        writer.write(pack_message(Message(OFPT_HELLO, xid=1)))
        await writer.drain()
        await reader.read(4096)                                    # HELLO của switch
        writer.write(pack_message(UNKNOWN)
                     + pack_message(Message(OFPT_ECHO_REQUEST, xid=6, body=b"hi")))
        await writer.drain()
        echo_reply.extend(MessageReader().feed(await reader.read(4096)))
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    session = OpenFlowSession(dpid=1)
    try:
        await asyncio.wait_for(session.connect("127.0.0.1", port), 3)
        await asyncio.wait_for(session.handshake(), 3)
        await asyncio.wait_for(session.serve(on_message=on_message), 3)
    finally:
        await session.close()
        server.close()
        await server.wait_closed()
    return echo_reply


def test_serve_passes_other_messages_to_handler_and_still_answers_echo():
    seen = []
    echo_reply = asyncio.run(_run(seen.append))
    assert seen == [UNKNOWN]                      # ECHO do phiên tự xử lý, không đến handler
    assert [(m.type, m.xid, m.body) for m in echo_reply] == [(OFPT_ECHO_REPLY, 6, b"hi")]


def test_serve_without_handler_ignores_other_messages():
    echo_reply = asyncio.run(_run(None))
    assert [(m.type, m.xid) for m in echo_reply] == [(OFPT_ECHO_REPLY, 6)]