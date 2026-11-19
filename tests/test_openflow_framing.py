import pytest

from sdnsim.openflow.header import (
    Message, pack_message, OFPT_HELLO, OFPT_ECHO_REQUEST,
)
from sdnsim.openflow.reader import MessageReader


def test_hello_is_exactly_8_bytes():
    assert pack_message(Message(OFPT_HELLO, xid=1)) == bytes([4, 0, 0, 8, 0, 0, 0, 1])


def test_single_message_roundtrip():
    msg = Message(OFPT_ECHO_REQUEST, xid=7, body=b"abc")
    assert MessageReader().feed(pack_message(msg)) == [msg]


def test_message_split_into_single_bytes():
    msg = Message(OFPT_ECHO_REQUEST, xid=7, body=b"abcdef")
    data = pack_message(msg)
    reader, out = MessageReader(), []
    for i in range(len(data)):
        out += reader.feed(data[i:i + 1])
    assert out == [msg]


def test_two_messages_in_one_chunk():
    a = Message(OFPT_HELLO, xid=1)
    b = Message(OFPT_ECHO_REQUEST, xid=2, body=b"xy")
    assert MessageReader().feed(pack_message(a) + pack_message(b)) == [a, b]


def test_partial_tail_is_kept_until_complete():
    a = Message(OFPT_HELLO, xid=1)
    b = Message(OFPT_ECHO_REQUEST, xid=2, body=b"hello")
    data = pack_message(a) + pack_message(b)
    reader = MessageReader()
    assert reader.feed(data[:-3]) == [a]      # b mới đến được một phần
    assert reader.feed(data[-3:]) == [b]


def test_length_below_header_is_rejected():
    with pytest.raises(ValueError):
        MessageReader().feed(bytes([4, 0, 0, 4, 0, 0, 0, 1]))