import asyncio

from sdnsim.openflow.agent import hello_handshake
from sdnsim.openflow.header import Message, pack_message, OFPT_HELLO
from sdnsim.openflow.reader import MessageReader


async def _with_fake_controller(client_coro_factory):
    """Dựng controller giả trên cổng ngẫu nhiên, chạy client, trả về (kết quả, gói server nhận)."""
    received = []

    async def handler(reader, writer):
        mr = MessageReader()
        while not received:
            data = await reader.read(4096)
            if not data:
                break
            received.extend(mr.feed(data))
        writer.write(pack_message(Message(OFPT_HELLO, xid=99)))
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        result = await asyncio.wait_for(client_coro_factory(port), timeout=3)
    finally:
        server.close()
        await server.wait_closed()
    return result, received


def test_hello_handshake_exchanges_hello():
    reply, received = asyncio.run(
        _with_fake_controller(lambda port: hello_handshake("127.0.0.1", port, xid=5))
    )
    assert reply.type == OFPT_HELLO and reply.xid == 99
    assert received[0].type == OFPT_HELLO
    assert received[0].xid == 5
    assert received[0].version == 4


def test_handshake_fails_when_controller_closes_without_hello():
    async def run():
        async def handler(reader, writer):
            writer.close()          # đóng ngay, không gửi gì
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            await asyncio.wait_for(hello_handshake("127.0.0.1", port), timeout=3)
        finally:
            server.close()
            await server.wait_closed()

    try:
        asyncio.run(run())
    except ConnectionError:
        return
    raise AssertionError("mong đợi ConnectionError")