"""CRC16/XMODEM (poly 0x1021, init 0x0000, no reflect, no xorout).

This is a straight transliteration of the bit-serial algorithm found in the
app's common/function/crc.js (CRC16_XMODEM), which is what the request
builder (DataPacker.js setCrc) and response validator (CheckCrc) both use.
"""


def crc16_xmodem(data: bytes) -> int:
    crc = 0
    for byte in data:
        for n in range(8):
            bit = (byte >> (7 - n)) & 1
            msb = (crc >> 15) & 1
            crc = (crc << 1) & 0xFFFF
            if msb ^ bit:
                crc ^= 0x1021
    return crc & 0xFFFF


def crc16_bytes(data: bytes) -> bytes:
    crc = crc16_xmodem(data)
    return bytes([(crc >> 8) & 0xFF, crc & 0xFF])
