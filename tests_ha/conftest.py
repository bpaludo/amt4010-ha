"""Home Assistant harness: a scripted AMT 4010 on loopback."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from custom_components.amt4010 import protocol as P

# Real capture, 2026-09-29 16:58 (see tests/_load.py).
FIELD_STATUS = bytes.fromhex(
    "00" * 24 + "416601000000103a1d091a00000000000f" + "00" * 13
)
NAMES = {1: "Porta Sala", 3: "Garagem", 5: "05Cozinha"}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def reply(data: bytes) -> bytes:
    body = bytes((len(data) + 1, P.ISEC_MOBILE)) + data
    return body + bytes((P.checksum(body),))


def command_of(frame: bytes) -> tuple[int, bytes]:
    i = 3
    while chr(frame[i]).isdigit():
        i += 1
    return frame[i], frame[i + 1 : -2]


@dataclass
class FakePanel:
    status: bytearray = field(default_factory=lambda: bytearray(FIELD_STATUS))
    names: dict[int, str] = field(default_factory=lambda: dict(NAMES))
    # command byte -> "ack", "drop", "nack:E4", ...; default: ACK for 41/44
    behaviour: dict[int, str] = field(default_factory=dict)
    # applied to the status when a command is acknowledged
    on_command: dict[tuple[int, bytes], dict[int, int]] = field(default_factory=dict)
    frames: list[tuple[int, bytes]] = field(default_factory=list)
    connections: int = 0
    port: int = 0

    def commands(self) -> list[int]:
        return [cmd for cmd, _ in self.frames]

    def _answer(self, command: int, content: bytes) -> bytes | None:
        rule = self.behaviour.get(command, "")
        if rule == "drop":
            return None
        if rule.startswith("nack:"):
            return reply(bytes((int(rule[5:], 16),)))
        if command == P.CMD_PARTIAL_STATUS:
            return reply(b"\xe5")
        if command == P.CMD_STATUS:
            return reply(bytes(self.status))
        if command == P.CMD_READ_EEPROM:
            address, quantity = (content[0] << 8) | content[1], content[2]
            first = (address - P.ZONE_NAMES_START) // 16 + 1
            data = b"".join(
                self.names.get(first + i, "").encode().ljust(16, b"\x00")
                if first + i in self.names
                else f"Zona {first + i:02d}".encode().ljust(16, b"\x00")  # fw 6.6 default
                for i in range(quantity // 16)
            )
            return reply(b"\x01" + data)
        if command in (P.CMD_ARM, P.CMD_DISARM):
            for offset, value in self.on_command.get((command, content), {}).items():
                self.status[offset] = value
            return reply(b"\xfe")
        return reply(b"\xe2")

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.connections += 1
        try:
            head = await reader.readexactly(1)
            frame = head + await reader.readexactly(head[0] + 1)
            command, content = command_of(frame)
            self.frames.append((command, content))
            answer = self._answer(command, content)
            if answer is not None:
                writer.write(answer)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


async def _start(panel: FakePanel):
    server = await asyncio.start_server(panel.handle, "127.0.0.1", 0)
    panel.port = server.sockets[0].getsockname()[1]
    return server


@pytest.fixture
async def fake_panel(socket_enabled):
    panel = FakePanel()
    server = await _start(panel)
    yield panel
    server.close()
    await server.wait_closed()


@pytest.fixture
async def second_panel(socket_enabled):
    panel = FakePanel()
    server = await _start(panel)
    yield panel
    server.close()
    await server.wait_closed()
