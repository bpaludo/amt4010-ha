"""Load the Home-Assistant-free modules without running the package __init__.

Registering a bare package object first lets ``from . import protocol`` work
inside client.py while ``custom_components/amt4010/__init__.py`` (which imports
Home Assistant) never runs.
"""
from __future__ import annotations

import asyncio
import importlib
import pathlib
import sys
import types
from dataclasses import dataclass, field

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "custom_components" / "amt4010"
_NAME = "amt4010_pure"

if _NAME not in sys.modules:
    package = types.ModuleType(_NAME)
    package.__path__ = [str(PACKAGE_DIR)]
    sys.modules[_NAME] = package

protocol = importlib.import_module(f"{_NAME}.protocol")
client = importlib.import_module(f"{_NAME}.client")
state = importlib.import_module(f"{_NAME}.state")

# Status 0x5B captured from the real AMT 4010 on 2026-09-29 16:58 (fw 6.6,
# partitioned, A-D disarmed, no zone open, no problem). No secret in it.
FIELD_STATUS = bytes.fromhex(
    "00" * 24 + "41 66 01 00 00 00 10 3a 1d 09 1a 00 00 00 00 00 0f".replace(" ", "") + "00" * 13
)


def reply(data: bytes) -> bytes:
    """A reply as the panel sends it: [len] E9 data [checksum]."""
    body = bytes((len(data) + 1, protocol.ISEC_MOBILE)) + data
    return body + bytes((protocol.checksum(body),))


def status_reply(status: bytes = FIELD_STATUS) -> bytes:
    return reply(status)


@dataclass
class FakePanel:
    """Loopback AMT 4010. ``script`` maps a command byte to a reply (bytes),
    to "drop" (close without answering) or to "silent" (never answer)."""

    script: dict = field(default_factory=dict)
    connections: int = 0
    frames: list = field(default_factory=list)
    port: int = 0
    server: asyncio.base_events.Server | None = None

    async def _handle(self, reader, writer):
        self.connections += 1
        try:
            head = await reader.readexactly(1)
            rest = await reader.readexactly(head[0] + 1)
            frame = head + rest
            self.frames.append(frame)
            command = _command_of(frame)
            action = self.script.get(command, reply(bytes((0xE2,))))
            if callable(action):
                action = action(frame)
            if action == "drop":
                return
            if action == "silent":
                await reader.read()  # never answer; wait for the client to give up
                return
            writer.write(action)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    def commands(self) -> list[int]:
        return [_command_of(f) for f in self.frames]


def _command_of(frame: bytes) -> int:
    """Frame: len E9 21 <pw> cmd [content] 21 chk; the password is digits."""
    i = 3
    while chr(frame[i]).isdigit():
        i += 1
    return frame[i]


def content_of(frame: bytes) -> bytes:
    i = 3
    while chr(frame[i]).isdigit():
        i += 1
    return frame[i + 1 : -2]
