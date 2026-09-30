"""Panel and partition states, including every contradiction -> unknown."""
from __future__ import annotations

import unittest

from _load import FIELD_STATUS, protocol, state


def status(ab: int = 0, cd: int = 0, general: int = 0, partitioned: int = 1):
    data = bytearray(FIELD_STATUS)
    data[26], data[27], data[28], data[29] = partitioned, ab, cd, general
    return protocol.parse_status(bytes(data))


AB = ["A", "B"]


class PanelState(unittest.TestCase):
    def test_table(self) -> None:
        cases = [
            # (ab, cd, general, partitions in use, expected)
            (0x00, 0, 0x00, AB, state.DISARMED),
            (0x00, 0, 0x08, AB, None),  # "armed" bit with nothing armed
            (0x03, 0, 0x08, AB, state.ARMED_AWAY),
            (0x03, 0, 0x00, AB, state.ARMED_AWAY),  # meaning of bit 3 unproven: not a test
            (0x01, 0, 0x08, AB, state.ARMED_HOME),  # partial
            (0x01, 0, 0x00, AB, state.ARMED_HOME),
            (0x13, 0, 0x08, AB, state.ARMED_HOME),  # all armed, A in stay
            (0x00, 1, 0x08, AB, None),  # C armed but not in use
            (0x03, 0, 0x02, AB, state.TRIGGERED),  # siren
            (0x00, 0, 0x04, AB, state.TRIGGERED),  # silent: zones firing, disarmed
            (0x01, 0, 0x00, ["A"], state.ARMED_AWAY),
            (0x20, 0, 0x00, AB, None),  # B in stay without its armed bit
            (0x00, 0x10, 0x08, AB, None),  # C in stay without armed, general armed
        ]
        for ab, cd, general, in_use, expected in cases:
            with self.subTest(ab=hex(ab), cd=cd, general=hex(general), in_use=in_use):
                self.assertEqual(state.panel_state(status(ab, cd, general), in_use), expected)

    def test_unpartitioned_uses_the_general_bit(self) -> None:
        self.assertEqual(state.panel_state(status(0, 0, 0x08, 0), AB), state.ARMED_AWAY)
        self.assertEqual(state.panel_state(status(0, 0, 0x00, 0), AB), state.DISARMED)
        # a partition bit says armed, the panel says disarmed: unknown, not "disarmed"
        self.assertIsNone(state.panel_state(status(0x01, 0, 0x00, 0), AB))
        self.assertEqual(state.panel_state(status(0x01, 0, 0x08, 0), AB), state.ARMED_AWAY)
        # stay unpartitioned: home, never away (contra-assinatura, achado 1)
        self.assertEqual(state.panel_state(status(0x11, 0, 0x08, 0), AB), state.ARMED_HOME)


class PartitionState(unittest.TestCase):
    def test_table(self) -> None:
        self.assertEqual(state.partition_state(status(0x01), "A"), state.ARMED_AWAY)
        self.assertEqual(state.partition_state(status(0x01), "B"), state.DISARMED)
        self.assertEqual(state.partition_state(status(0x22), "B"), state.ARMED_HOME)
        # contradictions: never "disarmed" (Codex, Bloco 2)
        self.assertIsNone(state.partition_state(status(0x20), "B"))  # stay without armed
        self.assertIsNone(state.partition_state(status(0x20), "A"))
        self.assertIsNone(state.partition_state(status(0x00, 0, 0x08), "A"))  # armed bit, nothing armed
        self.assertIsNone(state.partition_state(status(0x00, 0, 0x08), "B"))
        self.assertEqual(state.partition_state(status(0x00, 0, 0x02), "B"), state.TRIGGERED)
        # a partition entity of an unpartitioned panel has no state of its own
        for ab, general in ((0x00, 0x08), (0x01, 0x00), (0x00, 0x00), (0x01, 0x08)):
            self.assertIsNone(state.partition_state(status(ab, 0, general, 0), "A"))


class Zones(unittest.TestCase):
    def test_zone_list(self) -> None:
        self.assertEqual(state.parse_zone_list("1-3, 12;64"), [1, 2, 3, 12, 64])
        self.assertEqual(state.parse_zone_list(""), [])
        for bad in ("0", "65", "3-1", "a", "1-"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                state.parse_zone_list(bad)

    def test_known_zones(self) -> None:
        names = {1: "Sala", 2: None, 5: "Cozinha"}
        self.assertEqual(state.known_zones(names, ""), [1, 5])
        self.assertEqual(state.known_zones(names, "2-3"), [2, 3])
        for empty in ("   ", ",", " ; ", None):
            self.assertEqual(state.known_zones(names, empty), [1, 5])
        self.assertEqual(state.zone_label(1, names), "01 Sala")
        self.assertEqual(state.zone_label(2, names), "02")
        numbered = {1: "01Entrada", 5: "05 Sala", 11: "11-Casa", 9: "09", 3: "3 Sala"}
        self.assertEqual(
            [state.zone_label(z, numbered) for z in (1, 5, 11, 9, 3)],
            ["01Entrada", "05 Sala", "11-Casa", "09", "3 Sala"],
        )


if __name__ == "__main__":
    unittest.main()
