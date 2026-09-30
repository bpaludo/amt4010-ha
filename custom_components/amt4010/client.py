"""TCP client of the AMT 4010 — one connection per operation. No Home Assistant.

The panel's TCP port serves one client at a time, and the owner's app uses the
same port. So the client never keeps a connection: it opens, sends one frame,
reads the whole reply, and closes. A single lock serializes everything, so Home
Assistant never holds two connections to the panel at once.

``_exchange`` is the only method that writes to the socket. It runs the
allowlist before opening the connection; nothing else can reach the wire.

Logs carry the command byte, sizes and NACK codes only — never a frame, the
password or a code.
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging

from . import protocol
from .protocol import NotAllowed, ProtocolError, Status

_LOGGER = logging.getLogger(__name__)

CONNECT_TIMEOUT = 3.0
REPLY_TIMEOUT = 3.0
CLOSE_TIMEOUT = 1.0
MAX_BACKOFF_SECONDS = 60.0


class Amt4010Error(Exception):
    """Base error. Messages never carry frames, passwords or codes."""


class CannotConnect(Amt4010Error):
    """No trustworthy reply to a read."""


class NotSent(CannotConnect):
    """The connection failed before the frame left: nothing reached the panel."""


class BadReply(CannotConnect):
    """A reply arrived but failed the size, checksum or header checks."""


class InvalidAuth(Amt4010Error):
    """NACK E1: the panel refused the password."""


class NotAmt4010(Amt4010Error):
    """The panel answered, but not like an AMT 4010."""


class Rejected(Amt4010Error):
    """Any other NACK. The panel did not apply the command."""

    def __init__(self, code: int) -> None:
        self.code = code
        super().__init__(
            f"NACK 0x{code:02X}: {protocol.NACK_MESSAGES.get(code, 'unknown')}"
        )


class AmbiguousResult(Amt4010Error):
    """A command left and no valid reply came back: it may have been applied."""


__all__ = [
    "AmbiguousResult",
    "Amt4010Client",
    "Amt4010Error",
    "BadReply",
    "CannotConnect",
    "InvalidAuth",
    "NotAllowed",
    "NotAmt4010",
    "NotSent",
    "Rejected",
    "backoff_seconds",
    "failure_reason",
]


def backoff_seconds(base: float, failures: int, cap: float = MAX_BACKOFF_SECONDS) -> float:
    """Poll interval after ``failures`` consecutive failed cycles."""
    if failures <= 0:
        return base
    return min(base * 2**failures, cap)


def failure_reason(exc: BaseException) -> str:
    """Stable label for the health entity."""
    for kind, label in (
        (InvalidAuth, "invalid_auth"),
        (NotAmt4010, "not_amt4010"),
        (Rejected, "rejected"),
        (BadReply, "bad_reply"),
        (NotSent, "unreachable"),
    ):
        if isinstance(exc, kind):
            return label
    return "no_reply"


class Amt4010Client:
    def __init__(
        self,
        host: str,
        port: int,
        password: str,
        *,
        commands_enabled: bool = False,
        connect_timeout: float = CONNECT_TIMEOUT,
        reply_timeout: float = REPLY_TIMEOUT,
    ) -> None:
        if not protocol.valid_password(password):
            raise ValueError("the password must have 4 or 6 digits")
        self.host = host
        self.port = port
        self._password = password
        self._commands_enabled = commands_enabled
        self._connect_timeout = connect_timeout
        self._reply_timeout = reply_timeout
        self._lock = asyncio.Lock()
        # Latched by NACK E1 from any operation: nothing else is sent with this
        # password. A new client (entry reload after reauth) starts clean.
        self._password_refused = False

    def __repr__(self) -> str:
        state = "on" if self._commands_enabled else "off"
        return f"<Amt4010Client {self.host}:{self.port} commands={state}>"

    @property
    def commands_enabled(self) -> bool:
        return self._commands_enabled

    @property
    def password_refused(self) -> bool:
        return self._password_refused

    def password_matches(self, code: str | None) -> bool:
        """Constant-time check of a code typed in Home Assistant."""
        return code is not None and hmac.compare_digest(
            code.encode("utf-8"), self._password.encode("utf-8")
        )

    async def _exchange(
        self, command: int, content: bytes = b"", *, is_command: bool = False
    ) -> bytes:
        """Send one frame and return the validated reply data.

        The only method that writes to the panel. Refuses anything outside the
        allowlist before a connection exists.
        """
        protocol.check_allowed(command, content, self._commands_enabled)
        frame = protocol.build_frame(self._password, command, content)
        async with self._lock:
            # Checked inside the lock: an operation queued behind the one that
            # got E1 must not repeat the password either.
            if self._password_refused:
                raise InvalidAuth("the panel refused this password before; not repeated")
            writer: asyncio.StreamWriter | None = None
            try:
                try:
                    async with asyncio.timeout(self._connect_timeout):
                        reader, writer = await asyncio.open_connection(
                            self.host, self.port
                        )
                except (OSError, TimeoutError) as exc:
                    raise NotSent(
                        f"0x{command:02X}: cannot connect to {self.host}:{self.port}"
                    ) from exc
                # From here on the frame may reach the panel.
                try:
                    async with asyncio.timeout(self._reply_timeout):
                        writer.write(frame)
                        await writer.drain()
                        head = await reader.readexactly(1)
                        rest = await reader.readexactly(head[0] + 1)
                except (OSError, TimeoutError, asyncio.IncompleteReadError) as exc:
                    _LOGGER.debug("0x%02X: no complete reply", command)
                    if is_command:
                        raise AmbiguousResult(
                            f"0x{command:02X} was sent and no reply came back"
                        ) from exc
                    raise CannotConnect(f"0x{command:02X}: no complete reply") from exc
            finally:
                if writer is not None:
                    writer.close()
                    with contextlib.suppress(Exception):
                        async with asyncio.timeout(CLOSE_TIMEOUT):
                            await writer.wait_closed()
        raw = head + rest
        try:
            data = protocol.parse_reply(raw)
        except ProtocolError as exc:
            _LOGGER.debug("0x%02X: rejected a %d-byte reply: %s", command, len(raw), exc)
            if is_command:
                raise AmbiguousResult(
                    f"0x{command:02X} was sent and the reply is not valid"
                ) from exc
            raise BadReply(f"0x{command:02X}: {exc}") from exc
        _LOGGER.debug("0x%02X: %d-byte reply", command, len(data))
        return data

    def _raise_nack(self, command: int, data: bytes) -> None:
        """For one-byte replies: return on ACK, raise on NACK."""
        code = protocol.nack_code(data)
        if code is None:
            return
        _LOGGER.debug("0x%02X: NACK 0x%02X", command, code)
        if code == protocol.NACK_E1_PASSWORD:
            self._password_refused = True
            raise InvalidAuth("the panel refused the password")
        raise Rejected(code)

    async def detect(self) -> None:
        """Model check used by the config flow.

        The AMT 4010 answers the partial status (0x5A) with NACK E5 "command
        discontinued" (field, 2026-09-29): that answer is its signature. A
        43-byte status or any other NACK means another model.
        """
        data = await self._exchange(protocol.CMD_PARTIAL_STATUS)
        if len(data) != 1:
            raise NotAmt4010(f"0x5A answered with {len(data)} bytes of status")
        try:
            self._raise_nack(protocol.CMD_PARTIAL_STATUS, data)
        except ProtocolError as exc:
            raise NotAmt4010(str(exc)) from exc
        except Rejected as exc:
            if exc.code == protocol.NACK_E5_DISCONTINUED:
                return
            raise NotAmt4010(str(exc)) from exc
        raise NotAmt4010("0x5A was acknowledged instead of refused")

    async def get_status(self) -> Status:
        data = await self._exchange(protocol.CMD_STATUS)
        if len(data) == 1:
            try:
                self._raise_nack(protocol.CMD_STATUS, data)
            except ProtocolError as exc:
                raise BadReply(str(exc)) from exc
            raise BadReply("0x5B was acknowledged without a status")
        try:
            return protocol.parse_status(data)
        except ProtocolError as exc:
            raise BadReply(str(exc)) from exc

    async def read_zone_names(self) -> dict[int, str | None]:
        """The 64 names in 6 reads. All or nothing."""
        names: dict[int, str | None] = {}
        for address, quantity in protocol.zone_name_blocks():
            data = await self._exchange(
                protocol.CMD_READ_EEPROM,
                protocol.eeprom_read_content(address, quantity),
            )
            if len(data) == 1:
                try:
                    self._raise_nack(protocol.CMD_READ_EEPROM, data)
                except ProtocolError as exc:
                    raise BadReply(str(exc)) from exc
                raise BadReply("0x5C was acknowledged without data")
            try:
                eeprom = protocol.parse_eeprom_reply(data, quantity)
            except ProtocolError as exc:
                raise BadReply(str(exc)) from exc
            names.update(protocol.decode_zone_names(address, eeprom))
        return names

    async def arm(self, partition: str | None = None) -> None:
        await self._command(protocol.CMD_ARM, partition)

    async def disarm(self, partition: str | None = None) -> None:
        await self._command(protocol.CMD_DISARM, partition)

    async def _command(self, command: int, partition: str | None) -> None:
        """Never retried: a second arm/disarm on a lost reply could undo the first."""
        content = b"" if partition is None else bytes((protocol.PARTITIONS[partition],))
        data = await self._exchange(command, content, is_command=True)
        try:
            self._raise_nack(command, data)
        except ProtocolError as exc:
            raise AmbiguousResult(f"0x{command:02X}: {exc}") from exc
