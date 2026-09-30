"""Frames, parser and names — no network, no Home Assistant.

The protocol tests pin our reading of the Intelbras SDK spreadsheet
("SDKCentraisDeAlarmeIntelbras-v1.0.1", sheet "4-Comandar central via APP";
row numbers below refer to it) and of the field capture. They are change
detectors, not proof: only the real panel proves an offset.
"""
from __future__ import annotations

import datetime as dt
import unittest

from _load import FIELD_STATUS, protocol, reply

P = protocol


class T1FrameBuilder(unittest.TestCase):
    """Every example frame of the SDK, password 1234."""

    SDK_FRAMES = [
        # (row of the "Requisição software" cell, command, content, frame)
        (288, P.CMD_PARTIAL_STATUS, b"", "08 E9 21 31 32 33 34 5A 21 40"),
        (485, P.CMD_STATUS, b"", "08 E9 21 31 32 33 34 5B 21 41"),
        (820, P.CMD_READ_EEPROM, bytes((0x05, 0x1A, 0x01)), "0B E9 21 31 32 33 34 5C 05 1A 01 21 5B"),
        (862, P.CMD_ARM, b"", "08 E9 21 31 32 33 34 41 21 5B"),
        (873, P.CMD_ARM, bytes((0x50,)), "09 E9 21 31 32 33 34 41 50 21 0A"),
        (895, P.CMD_ARM, bytes((0x41,)), "09 E9 21 31 32 33 34 41 41 21 1B"),
        (906, P.CMD_ARM, bytes((0x42, 0x50)), "0A E9 21 31 32 33 34 41 42 50 21 4B"),
        (951, P.CMD_DISARM, b"", "08 E9 21 31 32 33 34 44 21 5E"),
        (962, P.CMD_DISARM, bytes((0x41,)), "09 E9 21 31 32 33 34 44 41 21 1E"),
    ]

    def test_sdk_examples(self) -> None:
        for row, command, content, expected in self.SDK_FRAMES:
            with self.subTest(row=row):
                frame = P.build_frame("1234", command, content)
                self.assertEqual(frame.hex(" ").upper(), expected)

    def test_sdk_ack_and_nack_checksums(self) -> None:
        # Rows 52-112: every ACK/NACK example, "02 E9 <code> <checksum>".
        examples = {0xFE: 0xEA, 0xE0: 0xF4, 0xE1: 0xF5, 0xE2: 0xF6, 0xE3: 0xF7,
                    0xE4: 0xF0, 0xE5: 0xF1, 0xE6: 0xF2, 0xE7: 0xF3, 0xE8: 0xFC,
                    0xEA: 0xFE}
        for code, check in examples.items():
            with self.subTest(code=hex(code)):
                self.assertEqual(reply(bytes((code,))), bytes((0x02, 0xE9, code, check)))
        self.assertEqual(set(examples) - {0xFE}, set(P.NACK_MESSAGES))

    def test_six_digit_password(self) -> None:
        frame = P.build_frame("123456", P.CMD_STATUS)
        self.assertEqual(frame[0], 0x0A)
        self.assertEqual(frame[3:9], b"123456")

    def test_bad_passwords_are_refused_without_echo(self) -> None:
        for bad in ("123", "12345", "1234567", "12a4", "", "１２３４"):
            with self.subTest(length=len(bad)):
                with self.assertRaises(ValueError) as ctx:
                    P.build_frame(bad, P.CMD_STATUS)
                if bad:
                    self.assertNotIn(bad, str(ctx.exception))


class T3StatusParser(unittest.TestCase):
    def test_field_capture(self) -> None:
        s = P.parse_status(FIELD_STATUS)
        self.assertEqual(s.model, 0x41)
        self.assertEqual(s.model_name, "AMT 4010")
        self.assertEqual(s.firmware_version, "6.6")
        self.assertTrue(s.partitioned)
        self.assertEqual(s.partitions_armed, dict.fromkeys("ABCD", False))
        self.assertEqual(s.partitions_stay, dict.fromkeys("ABCD", False))  # fw 6.6 >= 5.7
        self.assertEqual(s.clock, dt.datetime(2026, 9, 29, 16, 58))
        self.assertFalse(s.alarm_active or s.armed_flag or s.problem_flag)
        self.assertFalse(
            s.ac_failure or s.battery_problem or s.aux_overload or s.siren_wiring_problem
            or s.phone_line_cut or s.event_comm_failure or s.bus_problem or s.keypads_tampered
        )
        for zones in (s.open_zones, s.violated_zones, s.bypassed_zones, s.tamper_zones,
                      s.short_zones, s.low_battery_zones):
            self.assertEqual(zones, frozenset())
        self.assertEqual(s.battery_details()["icon_bars"], 3)  # 0x0f: outline + 3 bars
        self.assertTrue(s.battery_details()["icon_outline"])

    def _with(self, offset: int, value: int) -> P.Status:
        data = bytearray(FIELD_STATUS)
        data[offset] = value
        return P.parse_status(bytes(data))

    def test_each_offset_against_the_sdk(self) -> None:
        # (SDK row, 0-based offset, byte value, check)
        cases = [
            (321, 0, 0x01, lambda s: s.open_zones == {1}),
            (321, 0, 0x80, lambda s: s.open_zones == {8}),
            (335, 7, 0x80, lambda s: s.open_zones == {64}),
            (337, 8, 0x04, lambda s: s.violated_zones == {3}),
            (340, 16, 0x01, lambda s: s.bypassed_zones == {1}),
            (340, 23, 0x80, lambda s: s.bypassed_zones == {64}),
            (345, 24, 0x1E, lambda s: s.model_name == "AMT 2018 E/EG"),
            (353, 25, 0x31, lambda s: s.firmware_version == "3.1"),
            (355, 26, 0x00, lambda s: not s.partitioned),
            (361, 27, 0x01, lambda s: s.partitions_armed["A"] and not s.partitions_armed["B"]),
            (361, 27, 0x02, lambda s: s.partitions_armed["B"] and not s.partitions_armed["A"]),
            (366, 28, 0x01, lambda s: s.partitions_armed["C"]),
            (366, 28, 0x02, lambda s: s.partitions_armed["D"]),
            (370, 29, 0x01, lambda s: s.problem_flag and not s.alarm_active),
            (370, 29, 0x02, lambda s: s.siren_on and s.alarm_active),
            (370, 29, 0x04, lambda s: s.zones_firing and s.alarm_active and not s.siren_on),
            (370, 29, 0x08, lambda s: s.armed_flag and not s.alarm_active),
            (387, 35, 0x01, lambda s: s.ac_failure and not s.battery_problem),
            (387, 35, 0x02, lambda s: s.battery_problem and s.battery_details()["low"]),
            (387, 35, 0x04, lambda s: s.battery_details()["missing_or_reversed"]),
            (387, 35, 0x08, lambda s: s.battery_details()["short_circuit"]),
            (387, 35, 0x10, lambda s: s.aux_overload and not s.ac_failure),
            (397, 36, 0x01, lambda s: s.bus_problems()["keypads"] == (1,)),
            (397, 36, 0x80, lambda s: s.bus_problems()["receivers"] == (4,)),
            (400, 37, 0x01, lambda s: s.bus_problems()["pgm_expanders"] == (1,)),
            (400, 37, 0x10, lambda s: s.bus_problems()["zone_expanders"] == (1,)),
            (403, 38, 0x02, lambda s: s.bus_problems()["zone_expanders"] == (6,)),
            (412, 40, 0x03, lambda s: s.battery_details()["icon_bars"] == 1),
            (419, 41, 0x10, lambda s: s.keypads_tampered == (1,)),
            (419, 41, 0x0F, lambda s: s.keypads_tampered == ()),  # bits 0-3 N/A
            (425, 42, 0x01, lambda s: s.siren_wiring_problem),
            (425, 42, 0x02, lambda s: s.siren_wiring_problem),
            (425, 42, 0x04, lambda s: s.phone_line_cut and not s.siren_wiring_problem),
            (425, 42, 0x08, lambda s: s.event_comm_failure),
            (434, 43, 0x81, lambda s: s.tamper_zones == {1, 8}),
            (440, 44, 0x02, lambda s: s.short_zones == {2}),
            (445, 45, 0x04, lambda s: s.siren_bits()["siren_pgm_bit2_sdk"] and not s.siren_on),
            (445, 45, 0x08, lambda s: s.siren_bits()["siren_pgm_bit3_upstream"] and not s.siren_on),
            (445, 45, 0x40, lambda s: s.pgm_on == {1}),
            (445, 45, 0x10, lambda s: s.pgm_on == {3}),
            (453, 46, 0x01, lambda s: s.low_battery_zones == {17}),
            (463, 51, 0x80, lambda s: s.low_battery_zones == {64}),
            (469, 52, 0x01, lambda s: s.pgm_on == {4}),
            (469, 52, 0x80, lambda s: s.pgm_on == {11}),
            (471, 53, 0x01, lambda s: s.pgm_on == {12}),
            (471, 53, 0x80, lambda s: s.pgm_on == {19}),
        ]
        for row, offset, value, check in cases:
            with self.subTest(row=row, offset=offset, value=hex(value)):
                self.assertTrue(check(self._with(offset, value)))

    def test_stay_bits_only_from_firmware_5_7(self) -> None:
        stay = self._with(27, 0x11)
        self.assertTrue(stay.partitions_armed["A"] and stay.partitions_stay["A"])
        old = bytearray(FIELD_STATUS)
        old[25], old[27] = 0x56, 0x11
        self.assertIsNone(P.parse_status(bytes(old)).partitions_stay)

    def test_clock_is_binary_not_bcd(self) -> None:
        # SDK rows 375-384 say BCD; the field capture has minute 0x3a (58).
        self.assertEqual(P.parse_status(FIELD_STATUS).clock.minute, 58)
        self.assertIsNone(self._with(33, 13).clock)  # month 13: no clock, no crash

    def test_undocumented_partitioning_byte_is_refused(self) -> None:
        for value in (0x02, 0x80, 0xFF):
            with self.subTest(value=hex(value)), self.assertRaises(P.ProtocolError):
                self._with(26, value)

    def test_size_other_than_54_is_refused(self) -> None:
        for size in (0, 43, 53, 55, 143):
            with self.subTest(size=size), self.assertRaises(P.ProtocolError):
                P.parse_status(bytes(size))


class T4Replies(unittest.TestCase):
    def test_ack_and_nack(self) -> None:
        self.assertIsNone(P.nack_code(P.parse_reply(reply(b"\xfe"))))
        self.assertEqual(P.nack_code(P.parse_reply(reply(b"\xe1"))), 0xE1)
        with self.assertRaises(P.ProtocolError):
            P.nack_code(b"\x99")

    def test_bad_replies(self) -> None:
        good = reply(FIELD_STATUS)
        bad_checksum = good[:-1] + bytes(((good[-1] + 1) & 0xFF,))
        wrong_header = bytes((good[0], 0xE7)) + good[2:-1]
        wrong_header += bytes((P.checksum(wrong_header),))
        for name, raw in (
            ("checksum", bad_checksum),
            ("truncated", good[:-5]),
            ("trailing", good + b"\x00"),
            ("header", wrong_header),
            ("tiny", b"\x02\xe9"),
        ):
            with self.subTest(name=name), self.assertRaises(P.ProtocolError):
                P.parse_reply(raw)
        self.assertEqual(P.parse_reply(good), FIELD_STATUS)


class T9ZoneNames(unittest.TestCase):
    def test_blocks_cover_the_64_names(self) -> None:
        blocks = P.zone_name_blocks()
        self.assertEqual(
            blocks,
            [(0x0800, 0xC0), (0x08C0, 0xC0), (0x0980, 0xC0),
             (0x0A40, 0xC0), (0x0B00, 0xC0), (0x0BC0, 0x40)],
        )
        for address, quantity in blocks:
            P.check_allowed(P.CMD_READ_EEPROM, P.eeprom_read_content(address, quantity), False)

    def test_decode(self) -> None:
        records = [
            b"Porta Sala".ljust(16, b"\x00"),
            b"\x00" * 16,
            b"\xff" * 16,
            b"ABCDEFGHIJKLMN\x00\x00",  # factory pattern
            b"  Janela  \x00\x00\x00\x00\x00\x00",
            b"Garagem\x00lixo\x00\x00\x00\x00\x00",
        ]
        names = P.decode_zone_names(0x08C0, b"".join(records))
        self.assertEqual(
            names,
            {13: "Porta Sala", 14: None, 15: None, 16: None, 17: "Janela", 18: "Garagem"},
        )

    def test_default_name_of_firmware_6_6(self) -> None:
        # Field, 2026-09-29: unprogrammed zones read back as "Zona NN".
        records = [b"Zona 01", b"Zona 03", b"ZONA 3", b"03", b"Zona", b"Zona 1"]
        data = b"".join(r.ljust(16, b"\x00") for r in records)
        names = P.decode_zone_names(0x0800, data)
        self.assertEqual(
            names,
            {1: None, 2: "Zona 03", 3: None, 4: "03", 5: "Zona", 6: "Zona 1"},
        )

    def test_short_names_and_padding(self) -> None:
        # Contra-assinatura, achado 3 e observações: "01", "12", "23" are names;
        # 0xFF padding is not; latin-1 letters survive.
        records = [b"01", b"12", b"23", b"Zona 04\xff\xff\xff", b"Sal\xe3o", b"\xff" * 16,
                   b"ABCDEFGHIJKLMN"]
        data = b"".join(r[:16].ljust(16, b"\x00") for r in records)
        self.assertEqual(
            P.decode_zone_names(0x0800, data),
            {1: "01", 2: "12", 3: "23", 4: None, 5: "Salão", 6: None, 7: None},
        )

    def test_eeprom_reply_size(self) -> None:
        data = b"\x01" + b"x" * 0xC0
        self.assertEqual(P.parse_eeprom_reply(data, 0xC0), b"x" * 0xC0)
        with self.assertRaises(P.ProtocolError):
            P.parse_eeprom_reply(data[:-1], 0xC0)


class AllowlistUnit(unittest.TestCase):
    """The pure half of T2; the network half is in test_client."""

    def test_reads(self) -> None:
        P.check_allowed(P.CMD_STATUS, b"", False)
        P.check_allowed(P.CMD_PARTIAL_STATUS, b"", False)
        for bad in (b"\x00", b"\x08\x00\xc0\x00"):
            with self.assertRaises(P.NotAllowed):
                P.check_allowed(P.CMD_STATUS, bad, True)

    def test_eeprom_outside_the_names_is_refused(self) -> None:
        for address, quantity in ((0x051A, 1), (0x07FF, 1), (0x0BC0, 0x41),
                                  (0x0C00, 1), (0x0800, 0), (0x0800, 0xC1), (0x1800, 8)):
            with self.subTest(address=hex(address), quantity=quantity):
                with self.assertRaises(P.NotAllowed):
                    P.check_allowed(
                        P.CMD_READ_EEPROM, P.eeprom_read_content(address, quantity), True
                    )

    def test_commands(self) -> None:
        for command in (P.CMD_ARM, P.CMD_DISARM):
            with self.assertRaises(P.NotAllowed):
                P.check_allowed(command, b"", False)
            P.check_allowed(command, b"", True)
            for letter, byte in P.PARTITIONS.items():
                P.check_allowed(command, bytes((byte,)), True)
            for bad in (b"\x50", b"\x41\x50", b"\x45", b"\x00", b"\x41\x42"):
                with self.subTest(command=hex(command), content=bad.hex()):
                    with self.assertRaises(P.NotAllowed):
                        P.check_allowed(command, bad, True)

    def test_everything_else_is_refused(self) -> None:
        allowed = {P.CMD_PARTIAL_STATUS, P.CMD_STATUS, P.CMD_READ_EEPROM, P.CMD_ARM, P.CMD_DISARM}
        for command in range(256):
            if command in allowed:
                continue
            with self.assertRaises(P.NotAllowed):
                P.check_allowed(command, b"", True)


if __name__ == "__main__":
    unittest.main()
