"""Tests for MeshCoreClient.get_contacts and remove_contact methods."""

import struct

from meshcore_nomad_bridge.meshcore_client import MeshCoreClient

from openhop_core.companion.constants import (
    CONTACT_NAME_SIZE,
    MAX_PATH_SIZE,
    OUT_PATH_UNKNOWN,
    PUB_KEY_SIZE,
)


def _build_contact_frame(
    pubkey_hex="aa" * 32,
    name="TestNode",
    adv_type=0,
    flags=0,
    out_path_len=0,
    last_advert=1000,
    gps_lat=42.123456,
    gps_lon=-71.654321,
    lastmod=2000,
):
    """Build a contact body matching the wire format."""
    pubkey = bytes.fromhex(pubkey_hex)[:PUB_KEY_SIZE].ljust(PUB_KEY_SIZE, b"\x00")
    opl = OUT_PATH_UNKNOWN if out_path_len < 0 else min(out_path_len, 255)
    out_path = b"\x00" * MAX_PATH_SIZE
    name_bytes = name.encode("utf-8")[:CONTACT_NAME_SIZE].ljust(CONTACT_NAME_SIZE, b"\x00")
    return (
        pubkey
        + bytes([adv_type, flags, opl])
        + out_path
        + name_bytes
        + struct.pack("<I", last_advert)
        + struct.pack("<i", int(gps_lat * 1e6))
        + struct.pack("<i", int(gps_lon * 1e6))
        + struct.pack("<I", lastmod)
    )


def test_parse_contact_frame_basic():
    client = MeshCoreClient(host="127.0.0.1", port=5050)
    frame = _build_contact_frame()
    result = client._parse_contact_frame(frame)
    assert result is not None
    assert result["public_key"] == "aa" * 32
    assert result["name"] == "TestNode"
    assert result["adv_type"] == 0
    assert result["flags"] == 0
    assert result["out_path_len"] == 0
    assert result["last_advert"] == 1000
    assert abs(result["gps_lat"] - 42.123456) < 0.001
    assert abs(result["gps_lon"] - (-71.654321)) < 0.001
    assert result["lastmod"] == 2000


def test_parse_contact_frame_unknown_path():
    client = MeshCoreClient(host="127.0.0.1", port=5050)
    frame = _build_contact_frame(out_path_len=-1)
    result = client._parse_contact_frame(frame)
    assert result is not None
    assert result["out_path_len"] == -1


def test_parse_contact_frame_multi_hop():
    client = MeshCoreClient(host="127.0.0.1", port=5050)
    frame = _build_contact_frame(out_path_len=3)
    result = client._parse_contact_frame(frame)
    assert result is not None
    assert result["out_path_len"] == 3


def test_parse_contact_frame_repeater_type():
    client = MeshCoreClient(host="127.0.0.1", port=5050)
    frame = _build_contact_frame(adv_type=1, name="MyRepeater")
    result = client._parse_contact_frame(frame)
    assert result is not None
    assert result["adv_type"] == 1
    assert result["name"] == "MyRepeater"


def test_parse_contact_frame_empty_name():
    client = MeshCoreClient(host="127.0.0.1", port=5050)
    frame = _build_contact_frame(name="")
    result = client._parse_contact_frame(frame)
    assert result is not None
    assert result["name"] == ""


def test_parse_contact_frame_short_data_rejected():
    client = MeshCoreClient(host="127.0.0.1", port=5050)
    # Less than 131 bytes should return None
    result = client._parse_contact_frame(b"\x00" * 100)
    assert result is None


def test_parse_contact_frame_no_timestamps():
    """Frame with exactly the minimum 131 bytes (no trailing timestamps)."""
    client = MeshCoreClient(host="127.0.0.1", port=5050)
    pubkey = b"\xaa" * PUB_KEY_SIZE
    body = pubkey + bytes([0, 0, 0]) + b"\x00" * MAX_PATH_SIZE + b"Node\x00" + b"\x00" * (CONTACT_NAME_SIZE - 5)
    assert len(body) == 131
    result = client._parse_contact_frame(body)
    assert result is not None
    assert result["name"] == "Node"
    assert result["last_advert"] == 0
    assert result["gps_lat"] == 0.0
    assert result["lastmod"] == 0
