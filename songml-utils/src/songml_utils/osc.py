"""Minimal OSC 1.0 message codec (stdlib only).

Covers what the mixroom mix layer and its fake AbletonOSC exchange: int32 (``i``),
float32 (``f``), string (``s``), and the argument-less booleans ``T``/``F``. Bundles,
blobs and every other type tag are refused with ``OSCError``: an unknown tag means
the peer is not speaking the protocol this module was built for, and guessing would
hide that.
"""

from __future__ import annotations

import struct

type OSCArg = int | float | str | bool
type OSCMessage = tuple[str, list[OSCArg]]


class OSCError(ValueError):
    """A datagram that is not a well-formed OSC message of the supported types."""


def _pad(raw: bytes) -> bytes:
    return raw + b"\x00" * (4 - len(raw) % 4)


def _encode_string(text: str) -> bytes:
    return _pad(text.encode("utf-8"))


def encode_message(address: str, args: list[OSCArg] | tuple[OSCArg, ...] = ()) -> bytes:
    """Encode one OSC message."""
    if not address.startswith("/"):
        raise OSCError(f"OSC address must start with '/': {address!r}")
    tags = ","
    payload = b""
    for arg in args:
        # bool before int: bool is a subclass of int.
        if isinstance(arg, bool):
            tags += "T" if arg else "F"
        elif isinstance(arg, int):
            tags += "i"
            payload += struct.pack(">i", arg)
        elif isinstance(arg, float):
            tags += "f"
            payload += struct.pack(">f", arg)
        elif isinstance(arg, str):
            tags += "s"
            payload += _encode_string(arg)
        else:
            raise OSCError(f"unsupported OSC argument type {type(arg).__name__}: {arg!r}")
    return _encode_string(address) + _encode_string(tags) + payload


def _read_string(data: bytes, pos: int) -> tuple[str, int]:
    end = data.find(b"\x00", pos)
    if end < 0:
        raise OSCError("unterminated OSC string")
    text = data[pos:end].decode("utf-8")
    return text, end + (4 - (end - pos) % 4)


def decode_message(data: bytes) -> OSCMessage:
    """Decode one OSC message. Raises ``OSCError`` on anything else."""
    if data.startswith(b"#bundle"):
        raise OSCError("OSC bundles are not supported")
    address, pos = _read_string(data, 0)
    if not address.startswith("/"):
        raise OSCError(f"bad OSC address {address!r}")
    if pos >= len(data):
        return address, []
    tags, pos = _read_string(data, pos)
    if not tags.startswith(","):
        raise OSCError(f"bad OSC type tag string {tags!r}")
    args: list[OSCArg] = []
    for tag in tags[1:]:
        if tag == "i":
            if pos + 4 > len(data):
                raise OSCError("truncated int32")
            args.append(struct.unpack_from(">i", data, pos)[0])
            pos += 4
        elif tag == "f":
            if pos + 4 > len(data):
                raise OSCError("truncated float32")
            args.append(struct.unpack_from(">f", data, pos)[0])
            pos += 4
        elif tag == "s":
            text, pos = _read_string(data, pos)
            args.append(text)
        elif tag == "T":
            args.append(True)
        elif tag == "F":
            args.append(False)
        else:
            raise OSCError(f"unsupported OSC type tag {tag!r}")
    return address, args
