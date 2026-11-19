from .header import HEADER, HEADER_LEN, Message


class MessageReader:
    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[Message]:
        self._buffer.extend(data)
        messages: list[Message] = []

        while len(self._buffer) >= HEADER_LEN:
            version, message_type, length, xid = HEADER.unpack(
                bytes(self._buffer[:HEADER_LEN])
            )
            if length < HEADER_LEN:
                raise ValueError("OpenFlow message length cannot be below 8 bytes")
            if len(self._buffer) < length:
                break

            body = bytes(self._buffer[HEADER_LEN:length])
            del self._buffer[:length]
            messages.append(
                Message(
                    type=message_type,
                    xid=xid,
                    body=body,
                    version=version,
                )
            )

        return messages