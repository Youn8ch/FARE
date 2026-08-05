from __future__ import annotations

import os
import socket
from ipaddress import ip_address

import pytest


@pytest.fixture(autouse=True)
def block_unapproved_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep provisional evals offline unless the real-run gate is explicit."""
    if os.getenv("RUN_REAL_LLM_EVAL") == "1":
        return

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def is_loopback(address) -> bool:
        if not isinstance(address, tuple) or not address:
            return True
        try:
            return ip_address(address[0]).is_loopback
        except ValueError:
            return False

    def guarded_connect(sock, address):
        if is_loopback(address):
            return original_connect(sock, address)
        raise AssertionError("real network requires the approved real-eval gate")

    def guarded_connect_ex(sock, address):
        if is_loopback(address):
            return original_connect_ex(sock, address)
        raise AssertionError("real network requires the approved real-eval gate")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
