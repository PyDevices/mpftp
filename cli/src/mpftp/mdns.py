"""Find a board by its ``<hostname>.local`` name: a minimal mDNS client.

MicroPython's esp32 port answers mDNS A queries for ``network.hostname()``
(``mpy-esp32p4`` and the like). It doesn't advertise services, so there is
nothing to browse; the useful question is "what address has this name now?".

The operating system is asked first, because Windows 10+ and macOS resolve
``.local`` names themselves. When that fails (Linux without nss-mdns, WSL),
this sends one RFC 6762 "legacy unicast" query from an ordinary UDP port to
224.0.0.251:5353, and the responder answers that port directly. Standard
library only: the sidecar runs on whatever Python the user has.

This is best effort. From WSL in NAT mode multicast never leaves the VM, so use
the Windows-side Python (the sidecar), which is what the UIs do.
"""

from __future__ import annotations

import os
import socket
import struct
import time
from typing import Optional

MDNS_ADDR = "224.0.0.251"
MDNS_PORT = 5353
TYPE_A = 1
CLASS_IN = 1


def normalize(name: str) -> str:
    """``board`` or ``board.local`` or ``board.local.`` -> ``board.local``."""
    name = name.strip().rstrip(".").lower()
    if not name:
        raise ValueError("empty host name")
    if not name.endswith(".local"):
        name += ".local"
    return name


def build_query(name: str, query_id: int) -> bytes:
    """One question, type A, class IN. The id is echoed in a legacy-unicast reply."""
    header = struct.pack("!HHHHHH", query_id, 0, 1, 0, 0, 0)
    qname = b"".join(
        bytes([len(label)]) + label for label in (p.encode("idna") for p in name.split("."))
    )
    return header + qname + b"\x00" + struct.pack("!HH", TYPE_A, CLASS_IN)


def _read_name(msg: bytes, at: int) -> tuple[str, int]:
    """Decode a (possibly compressed) DNS name; return it and the offset after it."""
    labels: list[str] = []
    end: Optional[int] = None
    hops = 0
    while True:
        if at >= len(msg):
            raise ValueError("truncated name")
        n = msg[at]
        if n == 0:
            at += 1
            break
        if n & 0xC0 == 0xC0:
            if at + 1 >= len(msg):
                raise ValueError("truncated pointer")
            if end is None:
                end = at + 2
            at = ((n & 0x3F) << 8) | msg[at + 1]
            hops += 1
            if hops > 32:
                raise ValueError("name pointer loop")
            continue
        labels.append(msg[at + 1 : at + 1 + n].decode("utf-8", "replace"))
        at += 1 + n
    return ".".join(labels).lower(), (end if end is not None else at)


def parse_a_records(msg: bytes, name: str) -> list[str]:
    """IPv4 addresses the message gives for ``name``, from any record section."""
    if len(msg) < 12:
        return []
    _id, flags, qd, an, ns, ar = struct.unpack("!HHHHHH", msg[:12])
    if not flags & 0x8000:  # a query, not a response
        return []
    at = 12
    try:
        for _ in range(qd):
            _q, at = _read_name(msg, at)
            at += 4
        found: list[str] = []
        for _ in range(an + ns + ar):
            rname, at = _read_name(msg, at)
            rtype, _rclass, _ttl, rdlen = struct.unpack("!HHIH", msg[at : at + 10])
            at += 10
            rdata = msg[at : at + rdlen]
            at += rdlen
            if rtype == TYPE_A and rdlen == 4 and rname == name:
                ip = socket.inet_ntoa(rdata)
                if ip not in found:
                    found.append(ip)
        return found
    except (ValueError, struct.error):
        return []


def query(name: str, timeout: float = 1.5, attempts: int = 2) -> Optional[str]:
    """Ask the local link for ``name``'s IPv4 address with our own mDNS query."""
    name = normalize(name)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.bind(("", 0))
        per_try = timeout / max(1, attempts)
        for _ in range(max(1, attempts)):
            qid = int.from_bytes(os.urandom(2), "big")
            sock.sendto(build_query(name, qid), (MDNS_ADDR, MDNS_PORT))
            deadline = time.monotonic() + per_try
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                sock.settimeout(left)
                try:
                    data, _src = sock.recvfrom(9000)
                except socket.timeout:
                    break
                ips = parse_a_records(data, name)
                if ips:
                    return ips[0]
    except OSError:
        return None
    finally:
        sock.close()
    return None


def resolve(name: str, timeout: float = 1.5) -> dict[str, Optional[str]]:
    """``{"name", "ip", "via"}``: the OS resolver first, then our own query.

    ``via`` is ``"system"`` or ``"mdns"``, or None when neither found it.
    """
    name = normalize(name)
    try:
        info = socket.getaddrinfo(name, None, socket.AF_INET, socket.SOCK_STREAM)
        if info:
            return {"name": name, "ip": info[0][4][0], "via": "system"}
    except (OSError, UnicodeError):
        pass
    ip = query(name, timeout=timeout)
    return {"name": name, "ip": ip, "via": "mdns" if ip else None}
