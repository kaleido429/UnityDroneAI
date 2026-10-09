"""BLE frames: little-endian uint16 message ID, uint8 index/count, <=16 data bytes.
CBOR uses string keys in v1, integer millivolts/milliradians on the wire.
"""
import struct
import time
import cbor2


def frames(message: dict, message_id: int):
    payload = cbor2.dumps(message)
    if len(payload) > 1024:
        raise ValueError("Message exceeds 1024 bytes")
    count = (len(payload) + 15) // 16
    return [struct.pack("<HBB", message_id, i, count) + payload[i*16:(i+1)*16] for i in range(count)]


class Reassembler:
    def __init__(self):
        self.parts = {}
        self.recent = {}

    def feed(self, data: bytes):
        now = time.monotonic()
        self.parts = {k: v for k, v in self.parts.items() if now - v[0] < 2}
        self.recent = {k: v for k, v in self.recent.items() if now - v < 2}
        if not 5 <= len(data) <= 20:
            raise ValueError("Invalid frame length")
        mid, index, count = struct.unpack("<HBB", data[:4])
        if not 1 <= count <= 64 or index >= count:
            raise ValueError("Invalid fragment index/count")
        if mid in self.recent:
            return None
        if mid not in self.parts:
            if len(self.parts) >= 4:
                raise ValueError("Too many partial messages")
            self.parts[mid] = (now, count, {})
        _, expected, chunks = self.parts[mid]
        if count != expected or (index in chunks and chunks[index] != data[4:]):
            del self.parts[mid]
            raise ValueError("Conflicting fragment")
        chunks[index] = data[4:]
        if len(chunks) != count:
            return None
        del self.parts[mid]
        self.recent[mid] = now
        result = cbor2.loads(b"".join(chunks[i] for i in range(count)))
        if not isinstance(result, dict):
            raise ValueError("Expected CBOR map")
        return result
