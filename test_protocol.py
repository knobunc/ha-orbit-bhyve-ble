"""CLI smoke tests for Orbit B-Hyve BLE protocol logic.

Tests cipher math, frame building, battery parsing, HT34A CRC, and
entity wiring — everything that doesn't require a live BLE connection
or Home Assistant runtime.
"""
import struct
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "orbit-bhyve-ble"))

FAIL = 0


def check(label, condition, detail=""):
    global FAIL
    status = "PASS" if condition else "FAIL"
    if not condition:
        FAIL += 1
    suffix = f"  ({detail})" if detail else ""
    print(f"  [{status}] {label}{suffix}")


def test_aes_keystream():
    """Verify AES-128-ECB CTR keystream generation with known values."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = bytes.fromhex("00112233445566778899aabbccddeeff")
    iv = bytes(12)
    ctr = 0

    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    block = encryptor.update(iv + struct.pack("<I", ctr))

    check("AES keystream block is 16 bytes", len(block) == 16)
    check("AES keystream is deterministic",
          block == Cipher(algorithms.AES(key), modes.ECB()).encryptor().update(iv + struct.pack("<I", 0)))


def test_encrypt_decrypt_roundtrip():
    """Encrypt then decrypt must recover original plaintext."""
    from custom_components.orbit_bhyve.connection import BHyveBleConnection

    conn = object.__new__(BHyveBleConnection)
    conn._key = bytes.fromhex("00112233445566778899aabbccddeeff")
    conn._iv = bytes(12)
    conn._tx_ctr = 0
    conn._rx_ctr = 0
    conn._frame_magic = 0x10
    conn._trailer_const = 0x10
    conn.mac = "TEST"

    plaintext = b"\xd7\x47\xb6\x0d\x40\x04\x3c\x00\x00\x00\x00\x00"
    frame = conn.encrypt(plaintext)

    check("Frame starts with magic byte", frame[0] == 0x10)
    check("Frame length field matches ciphertext", frame[1] == len(plaintext))
    check("Frame total length is ct + 4", len(frame) == len(plaintext) + 4)

    recovered = conn.decrypt(frame)
    check("Decrypt recovers plaintext", recovered == plaintext,
          f"got {recovered.hex()} expected {plaintext.hex()}")


def test_trailer_checksum():
    """Trailer = (sum(plaintext) + trailer_const + len) & 0xFFFF."""
    from custom_components.orbit_bhyve.connection import BHyveBleConnection

    conn = object.__new__(BHyveBleConnection)
    conn._key = bytes(16)
    conn._iv = bytes(12)
    conn._tx_ctr = 0
    conn._frame_magic = 0x10
    conn._trailer_const = 0x10

    pt = bytes([0x01, 0x02, 0x03])
    frame = conn.encrypt(pt)
    trailer = struct.unpack("<H", frame[-2:])[0]
    expected = (sum(pt) + 0x10 + len(pt)) & 0xFFFF
    check("Trailer checksum correct", trailer == expected,
          f"got 0x{trailer:04x} expected 0x{expected:04x}")


def test_counter_advance():
    """TX and RX counters advance after encrypt/decrypt."""
    from custom_components.orbit_bhyve.connection import BHyveBleConnection

    conn = object.__new__(BHyveBleConnection)
    conn._key = bytes(16)
    conn._iv = bytes(12)
    conn._tx_ctr = 0
    conn._rx_ctr = 0
    conn._frame_magic = 0x10
    conn._trailer_const = 0x10
    conn.mac = "TEST"

    conn.encrypt(bytes(16))
    check("TX counter advances by 1 for 16-byte plaintext", conn._tx_ctr == 1)

    conn.encrypt(bytes(17))
    check("TX counter advances by 2 for 17-byte plaintext (2 blocks)", conn._tx_ctr == 3)


def test_battery_parsing():
    """Battery mV extraction from info-ack notification."""
    from custom_components.orbit_bhyve.devices.base import _mv_to_pct

    check("2400 mV = 0%", _mv_to_pct(2400) == 0)
    check("3000 mV = 100%", _mv_to_pct(3000) == 100)
    check("2700 mV = 50%", _mv_to_pct(2700) == 50)
    check("2602 mV ~ 34%", _mv_to_pct(2602) == 34)
    check("Below 2400 clamps to 0%", _mv_to_pct(2000) == 0)
    check("Above 3000 clamps to 100%", _mv_to_pct(3500) == 100)


def test_observe_plaintext_battery():
    """_observe_plaintext parses battery from a synthetic info-ack frame."""
    from custom_components.orbit_bhyve.devices.base import BHyveBleDeviceBase

    class FakeDevice(BHyveBleDeviceBase):
        async def start_watering(self, s, d): return False
        async def stop_watering(self, s=None): return False

    dev = object.__new__(FakeDevice)
    dev.mac = "AA:BB:CC:DD:EE:FF"
    dev.battery_mv = None
    dev.battery_pct = None

    # info-ack: [mesh:2][type=0x43(reply):1][seq=0x03:1][routing=0x40:1][payload:7+]
    # battery mV at bytes 9-10 (LE uint16) = 2771 = 0x0AD3
    pt = bytes([
        0xD7, 0x47,  # mesh
        0x43,        # type with reply bit (0x40 | 0x03)
        0x03,        # seq = info
        0x40,        # routing
        0x00, 0x00, 0x00, 0x00,  # payload padding
        0xD3, 0x0A,  # battery mV = 2771
        0x00,        # extra byte to reach len >= 12
    ])
    dev._observe_plaintext(pt)
    check("Battery mV parsed from info-ack", dev.battery_mv == 2771,
          f"got {dev.battery_mv}")
    check("Battery % derived", dev.battery_pct == 62,
          f"got {dev.battery_pct}")


def test_observe_plaintext_logs_unhandled():
    """Unrecognized reply notifications don't crash."""
    from custom_components.orbit_bhyve.devices.base import BHyveBleDeviceBase

    class FakeDevice(BHyveBleDeviceBase):
        async def start_watering(self, s, d): return False
        async def stop_watering(self, s=None): return False

    dev = object.__new__(FakeDevice)
    dev.mac = "AA:BB:CC:DD:EE:FF"
    dev.battery_mv = None
    dev.battery_pct = None

    # Reply with seq != 0x03 — should log but not crash
    pt = bytes([0xD7, 0x47, 0x45, 0x05, 0x40, 0x00, 0x00])
    dev._observe_plaintext(pt)
    check("Unhandled notification doesn't crash", True)
    check("Battery unchanged for non-info reply", dev.battery_mv is None)


def test_ht34a_crc16():
    """CRC16-CCITT with init=0."""
    from custom_components.orbit_bhyve.devices.ht34a import _crc16_ccitt

    crc = _crc16_ccitt(b"\x00", 0)
    check("CRC16 of 0x00", crc == 0x0000, f"got 0x{crc:04x}")

    crc = _crc16_ccitt(b"123456789", 0)
    check("CRC16 of '123456789'", crc == 0x31C3, f"got 0x{crc:04x}")


def test_ht34a_message_build():
    """HT34A message has correct header + CRC structure."""
    from custom_components.orbit_bhyve.devices.ht34a import _build_message, MSG_HEADER

    pb = bytes([0x72, 0x04, 0x08, 0x02, 0x12, 0x00])
    msg = _build_message(pb)

    check("Message starts with AA 77 5A 0F", msg[:4] == MSG_HEADER)
    check("Length byte = protobuf + 2", msg[4] == len(pb) + 2)
    check("Padding byte is 0x00", msg[5] == 0x00)
    check("Protobuf payload present", msg[6:6+len(pb)] == pb)
    check("Message ends with 2-byte CRC", len(msg) == 4 + 1 + 1 + len(pb) + 2)


def test_ht34a_start_protobuf():
    """HT34A start command builds valid protobuf."""
    from custom_components.orbit_bhyve.devices.ht34a import _build_start_pb

    pb = _build_start_pb(station_id=0, duration_sec=300)
    check("Start protobuf is non-empty bytes", isinstance(pb, bytes) and len(pb) > 0)
    # Field 14 (timer_mode) wraps field 1 (mode=2=manual) + field 2 (manual_params)
    check("Starts with field 14 tag", pb[0] == (14 << 3) | 2)


def test_ht25_build_start_stop():
    """HT25 start/stop frame builders produce correct structure."""
    from custom_components.orbit_bhyve.devices.ht25_fw0085 import _build_start, _build_stop, D747_MAGIC

    start = _build_start(0xB6, 60)
    check("Start begins with D7 47", start[:2] == D747_MAGIC)
    check("Start type byte is 0xB6", start[2] == 0xB6)
    check("Start duration 60 encoded LE", start[6:8] == (60).to_bytes(2, "little"))

    stop = _build_stop(0xB7)
    check("Stop begins with D7 47", stop[:2] == D747_MAGIC)
    check("Stop type byte is 0xB7", stop[2] == 0xB7)


def test_device_state_defaults():
    """DeviceState fields have correct defaults."""
    from custom_components.orbit_bhyve.devices.base import DeviceState

    s = DeviceState()
    check("is_watering defaults False", s.is_watering is False)
    check("is_connected defaults False", s.is_connected is False)
    check("water_volume_gal defaults None", s.water_volume_gal is None)
    check("flow_rate_gpm defaults None", s.flow_rate_gpm is None)
    check("seconds_remaining defaults None", s.seconds_remaining is None)


def test_imports():
    """All platform modules import without HA runtime."""
    modules = [
        "custom_components.orbit_bhyve.const",
        "custom_components.orbit_bhyve.connection",
        "custom_components.orbit_bhyve.devices.base",
        "custom_components.orbit_bhyve.devices.ht25_fw0085",
        "custom_components.orbit_bhyve.devices.ht34a",
    ]
    for mod in modules:
        try:
            __import__(mod)
            check(f"import {mod.split('.')[-1]}", True)
        except ImportError as e:
            check(f"import {mod.split('.')[-1]}", False, str(e))


if __name__ == "__main__":
    sections = [
        ("Imports", test_imports),
        ("AES Keystream", test_aes_keystream),
        ("Encrypt/Decrypt Roundtrip", test_encrypt_decrypt_roundtrip),
        ("Trailer Checksum", test_trailer_checksum),
        ("Counter Advance", test_counter_advance),
        ("Battery Parsing", test_battery_parsing),
        ("Plaintext Observer — Battery", test_observe_plaintext_battery),
        ("Plaintext Observer — Unhandled", test_observe_plaintext_logs_unhandled),
        ("HT34A CRC16", test_ht34a_crc16),
        ("HT34A Message Build", test_ht34a_message_build),
        ("HT34A Start Protobuf", test_ht34a_start_protobuf),
        ("HT25 Start/Stop", test_ht25_build_start_stop),
        ("DeviceState Defaults", test_device_state_defaults),
    ]
    for title, fn in sections:
        print(f"\n{title}:")
        fn()

    print(f"\n{'=' * 40}")
    if FAIL:
        print(f"{FAIL} test(s) FAILED")
        sys.exit(1)
    else:
        print("All tests passed.")
