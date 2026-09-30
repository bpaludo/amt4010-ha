"""The client against a loopback AMT 4010 — T2, T4, T5, T6, T7, T11, T12."""
from __future__ import annotations

import asyncio
import compileall
import io
import json
import logging
import re
import subprocess
import unittest

from _load import FIELD_STATUS, PACKAGE_DIR, FakePanel, client, content_of, protocol, reply

P = protocol
PASSWORD = "9876"
PASSWORD_HEX = ("39 38 37 36", "39383736")
ACK = reply(b"\xfe")
E1 = reply(b"\xe1")
E4 = reply(b"\xe4")
E5 = reply(b"\xe5")


class PanelCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.panel = await FakePanel(script={P.CMD_STATUS: reply(FIELD_STATUS)}).start()

    async def asyncTearDown(self) -> None:
        await self.panel.stop()

    def make(self, commands: bool = False, **kw) -> client.Amt4010Client:
        return client.Amt4010Client(
            "127.0.0.1", self.panel.port, PASSWORD, commands_enabled=commands, **kw
        )


class T2Allowlist(PanelCase):
    NEVER = [
        (0x41, bytes((0x41, 0x50))),  # stay
        (0x42, b""), (0x43, b""), (0x45, b"\x01"), (0x50, b"\x44\x31"),
        (0x63, b""), (0x5D, b""), (0xE7, b""),
        (0x5C, bytes((0x05, 0x1A, 0x01))),  # SDK example address, outside the names
        (0x5C, bytes((0x18, 0x00, 0x08))),  # event log
        (0x5C, bytes((0x0B, 0xC0, 0xC0))),  # runs past 0x0BFF
    ]

    async def test_commands_disabled_never_connect(self) -> None:
        c = self.make(commands=False)
        for command, content in ((0x41, b""), (0x41, b"\x41"), (0x44, b""), (0x44, b"\x41")):
            with self.subTest(command=hex(command), content=content.hex()):
                with self.assertRaises(P.NotAllowed):
                    await c._exchange(command, content, is_command=True)
        with self.assertRaises(P.NotAllowed):
            await c.arm("A")
        with self.assertRaises(P.NotAllowed):
            await c.disarm()
        self.assertEqual(self.panel.connections, 0)

    async def test_commands_enabled_pass(self) -> None:
        self.panel.script.update({P.CMD_ARM: ACK, P.CMD_DISARM: ACK})
        c = self.make(commands=True)
        await c.arm("A")
        await c.disarm("A")
        self.assertEqual(self.panel.connections, 2)
        self.assertEqual(
            [(f"{cmd:02x}", content_of(f).hex()) for cmd, f in zip(self.panel.commands(), self.panel.frames)],
            [("41", "41"), ("44", "41")],
        )

    async def test_never_allowed_even_with_commands(self) -> None:
        c = self.make(commands=True)
        for command, content in self.NEVER:
            with self.subTest(command=hex(command), content=content.hex()):
                with self.assertRaises(P.NotAllowed):
                    await c._exchange(command, content, is_command=True)
        self.assertEqual(self.panel.connections, 0)


class T4Nack(PanelCase):
    async def test_wrong_password_on_status_is_auth_without_retry(self) -> None:
        self.panel.script[P.CMD_STATUS] = E1
        with self.assertRaises(client.InvalidAuth):
            await self.make().get_status()
        self.assertEqual(self.panel.connections, 1)

    async def test_refused_password_is_never_sent_again(self) -> None:
        """E1 from any operation latches the client (Codex, Bloco 2, round 2)."""
        self.panel.script.update({P.CMD_STATUS: E1, P.CMD_ARM: ACK, P.CMD_READ_EEPROM: E1})
        c = self.make(commands=True)
        with self.assertRaises(client.InvalidAuth):
            await c.get_status()
        self.assertTrue(c.password_refused)
        for call in (c.get_status(), c.arm("A"), c.disarm(), c.read_zone_names(), c.detect()):
            with self.assertRaises(client.InvalidAuth):
                await call
        self.assertEqual(self.panel.connections, 1)

    async def test_refusal_from_a_name_read_latches_too(self) -> None:
        self.panel.script[P.CMD_READ_EEPROM] = E1
        c = self.make()
        with self.assertRaises(client.InvalidAuth):
            await c.read_zone_names()
        with self.assertRaises(client.InvalidAuth):
            await c.get_status()
        self.assertEqual(self.panel.connections, 1)

    async def test_e5_on_5a_is_the_4010(self) -> None:
        self.panel.script[P.CMD_PARTIAL_STATUS] = E5
        await self.make().detect()

    async def test_other_answers_to_5a_are_not_the_4010(self) -> None:
        for answer in (reply(b"\xe2"), ACK, reply(bytes(43))):
            with self.subTest(answer=answer[:3].hex()):
                self.panel.script[P.CMD_PARTIAL_STATUS] = answer
                with self.assertRaises(client.NotAmt4010):
                    await self.make().detect()

    async def test_e1_on_5a_is_auth(self) -> None:
        self.panel.script[P.CMD_PARTIAL_STATUS] = E1
        with self.assertRaises(client.InvalidAuth):
            await self.make().detect()

    async def test_bad_status_replies(self) -> None:
        good = reply(FIELD_STATUS)
        for name, answer in (
            ("checksum", good[:-1] + bytes(((good[-1] ^ 0x01),))),
            ("size", reply(FIELD_STATUS[:53])),
            ("other nack", reply(b"\xe2")),
        ):
            with self.subTest(name=name):
                self.panel.script[P.CMD_STATUS] = answer
                with self.assertRaises((client.BadReply, client.Rejected)):
                    await self.make().get_status()

    async def test_unreachable_and_silent(self) -> None:
        port = self.panel.port
        await self.panel.stop()
        c = client.Amt4010Client("127.0.0.1", port, PASSWORD, connect_timeout=0.5)
        with self.assertRaises(client.NotSent):
            await c.get_status()
        self.panel = await FakePanel(script={P.CMD_STATUS: "silent"}).start()
        c = self.make(reply_timeout=0.3)
        with self.assertRaises(client.CannotConnect):
            await c.get_status()


class T5NoLeak(PanelCase):
    async def test_password_never_in_logs_reprs_or_errors(self) -> None:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger(client.__name__)
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        texts: list[str] = []
        try:
            c = self.make(commands=True, reply_timeout=0.3)
            texts.append(repr(c))
            await c.get_status()
            self.panel.script[P.CMD_ARM] = ACK
            await c.arm("A")
            for script, call in (
                ({P.CMD_ARM: E4}, c.arm()),
                ({P.CMD_DISARM: "drop"}, c.disarm("B")),
                ({P.CMD_STATUS: E1}, c.get_status()),
                ({P.CMD_STATUS: reply(FIELD_STATUS)[:-1] + b"\x00"}, c.get_status()),
                ({P.CMD_STATUS: "silent"}, c.get_status()),
            ):
                self.panel.script.update(script)
                try:
                    await call
                except client.Amt4010Error as exc:
                    chain = exc
                    while chain is not None:
                        texts += [str(chain), repr(chain)]
                        chain = chain.__cause__
            try:
                await c._exchange(0x42, b"")
            except P.NotAllowed as exc:
                texts += [str(exc), repr(exc)]
            try:
                client.Amt4010Client("h", 1, "98765")
            except ValueError as exc:
                texts += [str(exc), repr(exc)]
        finally:
            logger.removeHandler(handler)
        texts.append(stream.getvalue())
        self.assertIn("0x41", stream.getvalue())  # the log is not empty
        blob = "\n".join(texts)
        for secret in (PASSWORD, "98765", *PASSWORD_HEX):
            self.assertNotIn(secret, blob)

    def test_code_check_is_local(self) -> None:
        c = client.Amt4010Client("h", 1, PASSWORD)
        self.assertTrue(c.password_matches(PASSWORD))
        for bad in (None, "", "1234", "98765", " 9876"):
            self.assertFalse(c.password_matches(bad))


class T6Backoff(unittest.TestCase):
    def test_backoff_grows_to_60(self) -> None:
        self.assertEqual(
            [client.backoff_seconds(5, n) for n in range(7)], [5, 10, 20, 40, 60, 60, 60]
        )
        self.assertEqual(client.backoff_seconds(60, 1), 60)


class T7Commands(PanelCase):
    async def test_drop_after_command_is_ambiguous_and_not_retried(self) -> None:
        self.panel.script[P.CMD_ARM] = "drop"
        c = self.make(commands=True)
        with self.assertRaises(client.AmbiguousResult):
            await c.arm("A")
        self.assertEqual(self.panel.connections, 1)
        self.assertEqual(self.panel.commands(), [P.CMD_ARM])

    async def test_silence_after_command_is_ambiguous(self) -> None:
        self.panel.script[P.CMD_DISARM] = "silent"
        with self.assertRaises(client.AmbiguousResult):
            await self.make(commands=True, reply_timeout=0.3).disarm()
        self.assertEqual(self.panel.connections, 1)

    async def test_nack_is_a_clear_refusal(self) -> None:
        self.panel.script[P.CMD_ARM] = E4
        with self.assertRaises(client.Rejected) as ctx:
            await self.make(commands=True).arm()
        self.assertEqual(ctx.exception.code, 0xE4)
        self.assertEqual(self.panel.connections, 1)

    async def test_garbage_after_command_is_ambiguous(self) -> None:
        self.panel.script[P.CMD_ARM] = reply(b"\x99")
        with self.assertRaises(client.AmbiguousResult):
            await self.make(commands=True).arm()

    async def test_one_connection_at_a_time(self) -> None:
        active = 0
        peak = 0

        async def slow(frame):
            nonlocal active, peak
            return reply(FIELD_STATUS)

        original = self.panel._handle

        async def counting(reader, writer):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.05)
            try:
                await original(reader, writer)
            finally:
                active -= 1

        self.panel.server.close()
        await self.panel.server.wait_closed()
        self.panel.server = await asyncio.start_server(counting, "127.0.0.1", self.panel.port)
        c = self.make()
        await asyncio.gather(*(c.get_status() for _ in range(5)))
        self.assertEqual(peak, 1)
        self.assertEqual(self.panel.connections, 5)


class T9NamesOverTheWire(PanelCase):
    async def test_six_reads(self) -> None:
        def names(frame):
            content = content_of(frame)
            address, quantity = (content[0] << 8) | content[1], content[2]
            first = (address - 0x0800) // 16 + 1
            data = b"".join(
                (f"Zona teste {first + i}".encode() if first + i in (1, 13, 64) else b"")
                .ljust(16, b"\x00")
                for i in range(quantity // 16)
            )
            return reply(b"\x01" + data)

        self.panel.script[P.CMD_READ_EEPROM] = names
        result = await self.make().read_zone_names()
        self.assertEqual(len(result), 64)
        self.assertEqual({z for z, n in result.items() if n}, {1, 13, 64})
        self.assertEqual(self.panel.connections, 6)

    async def test_nack_stops_the_read(self) -> None:
        self.panel.script[P.CMD_READ_EEPROM] = reply(b"\xe2")
        with self.assertRaises(client.Rejected):
            await self.make().read_zone_names()
        self.assertEqual(self.panel.connections, 1)


class T11Static(unittest.TestCase):
    """The plan's grep. Every hit must be one of these three, none of which
    builds a frame: the partition map (41-44 are partition letters A-D) and
    two NACK tables (E7 is a NACK the panel sends, "may not disarm")."""

    EXPLAINED = [
        re.compile(r"/protocol\.py:\d+:PARTITIONS: dict\[str, int\] = "
                   r"\{\"A\": 0x41, \"B\": 0x42, \"C\": 0x43, \"D\": 0x44\}$"),
        re.compile(r"/protocol\.py:\d+:    0xE7: \"user may not disarm\",$"),
        re.compile(r"/alarm_control_panel\.py:\d+:    0xE7: \"no_permission\",$"),
    ]

    def test_grep(self) -> None:
        hits = subprocess.run(
            ["grep", "-rnE", r"start_server|0xE7|0xe7|send_raw|0x45|0x50|0x42|0x43|0x63",
             str(PACKAGE_DIR), "--include=*.py"],
            capture_output=True, text=True,
        ).stdout.strip().splitlines()
        unexplained = [h for h in hits if not any(p.search(h) for p in self.EXPLAINED)]
        self.assertEqual(unexplained, [])
        self.assertEqual(len(hits), len(self.EXPLAINED))


class T12Package(unittest.TestCase):
    def test_compiles(self) -> None:
        self.assertTrue(compileall.compile_dir(str(PACKAGE_DIR), quiet=1, force=True))

    def test_json_and_translation_keys(self) -> None:
        manifest = json.loads((PACKAGE_DIR / "manifest.json").read_text())
        self.assertEqual(manifest["domain"], "amt4010")
        self.assertEqual(manifest["requirements"], [])
        base = json.loads((PACKAGE_DIR / "strings.json").read_text())

        def keys(node, prefix=""):
            if isinstance(node, dict):
                for k, v in node.items():
                    yield from keys(v, f"{prefix}/{k}")
            else:
                yield prefix

        for path in (PACKAGE_DIR / "translations").glob("*.json"):
            with self.subTest(path=path.name):
                self.assertEqual(set(keys(json.loads(path.read_text()))), set(keys(base)))


if __name__ == "__main__":
    unittest.main()
