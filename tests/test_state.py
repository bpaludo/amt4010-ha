"""Panel and partition states, including every contradiction -> unknown."""
from __future__ import annotations

import unittest

from _load import FIELD_STATUS, protocol, state


def status(ab: int = 0, cd: int = 0, general: int = 0, partitioned: int = 1,
           violated: tuple[int, ...] = (), opened: tuple[int, ...] = ()):
    data = bytearray(FIELD_STATUS)
    data[26], data[27], data[28], data[29] = partitioned, ab, cd, general
    for offset, zones in ((8, violated), (0, opened)):
        for zone in zones:
            data[offset + (zone - 1) // 8] |= 1 << ((zone - 1) % 8)
    return protocol.parse_status(bytes(data))


AB = ["A", "B"]
ABC = ["A", "B", "C"]
LIVE_A = state.AlarmView(
    siren=False, alarmed=frozenset("A"), new_zones=frozenset(), fire_event=False
)
SIREN = state.AlarmView(siren=True, alarmed=frozenset(), new_zones=frozenset(), fire_event=False)

# AMT 4010 fw 6.6, field capture (diagnostics, no extra connection), with the
# clock and the zone number replaced: the panel disarmed more than a day after
# an alarm, no zone open, one zone still in the violated map, general 0x44
# (bits 2 and 6).
FIELD_MEMORY = bytes.fromhex(
    "00 00 00 00 00 00 00 00 04 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 41 66 01 00"
    " 00 44 10 3a 1d 09 1a 00 00 00 00 00 0f 00 00 00 00 00 00 00 00 00 00 00 00 00"
)


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
            # no bit of the status is "alarm now": only the tracker's view is
            (0x03, 0, 0x02, AB, state.ARMED_AWAY),  # siren bit, not confirmed
            (0x00, 0, 0x04, AB, state.DISARMED),  # bit 2 alone (the 0.1.0 defect)
            (0x00, 0, 0x44, AB, state.DISARMED),  # bits 2 + 6: memory
            (0x01, 0, 0x00, ["A"], state.ARMED_AWAY),
            (0x20, 0, 0x00, AB, None),  # B in stay without its armed bit
            (0x00, 0x10, 0x08, AB, None),  # C in stay without armed, general armed
        ]
        for ab, cd, general, in_use, expected in cases:
            with self.subTest(ab=hex(ab), cd=cd, general=hex(general), in_use=in_use):
                self.assertEqual(state.panel_state(status(ab, cd, general), in_use), expected)

    def test_alarm_view_decides_triggered(self) -> None:
        self.assertEqual(state.panel_state(status(0x01), AB, LIVE_A), state.TRIGGERED)
        # a confirmed siren is an alarm even disarmed (24 h zone, panic)
        self.assertEqual(state.panel_state(status(0x00), AB, SIREN), state.TRIGGERED)

    def test_field_memory_is_not_triggered(self) -> None:
        s = protocol.parse_status(FIELD_MEMORY)
        self.assertEqual((s.general, s.violated_zones, s.open_zones), (0x44, {3}, frozenset()))
        self.assertTrue(s.alarm_memory and s.zones_firing and s.trigger_latched)
        tracker = state.AlarmTracker()
        view = tracker.update(s, 0.0)
        self.assertEqual(state.panel_state(s, ABC, view), state.DISARMED)
        for letter in ABC:
            self.assertEqual(state.partition_state(s, letter, view), state.DISARMED)
        self.assertFalse(view.fire_event)

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
        # triggered only while armed: the 0.1.0 defect showed it disarmed
        self.assertEqual(state.partition_state(status(0x00, 0, 0x02), "B"), state.DISARMED)
        self.assertEqual(state.partition_state(status(0x00, 0, 0x44), "A"), state.DISARMED)
        self.assertEqual(state.partition_state(status(0x01), "A", LIVE_A), state.TRIGGERED)
        self.assertEqual(state.partition_state(status(0x03), "B", LIVE_A), state.ARMED_AWAY)
        self.assertEqual(state.partition_state(status(0x00), "B", SIREN), state.DISARMED)
        self.assertEqual(state.partition_state(status(0x02), "B", SIREN), state.TRIGGERED)
        # a partition entity of an unpartitioned panel has no state of its own
        for ab, general in ((0x00, 0x08), (0x01, 0x00), (0x00, 0x00), (0x01, 0x08)):
            self.assertIsNone(state.partition_state(status(ab, 0, general, 0), "A"))


class Tracker(unittest.TestCase):
    """state.AlarmTracker: alarm now versus memory, one event per episode."""

    def setUp(self) -> None:
        self.tracker = state.AlarmTracker()
        self.now = 1000.0
        self.tracker.update(status(), self.now)  # first read: initializes

    def read(self, s, dt: float = 5.0) -> state.AlarmView:
        self.now += dt
        return self.tracker.update(s, self.now)

    def test_intrusion_while_armed(self) -> None:
        self.assertFalse(self.read(status(0x01)).live)  # A armed
        view = self.read(status(0x01, 0, 0x44, violated=(17,), opened=(17,)))
        self.assertTrue(view.live and view.fire_event)
        self.assertEqual((view.alarmed, view.new_zones), ({"A"}, {17}))
        # stays triggered while armed, with no second event for the same zone
        view = self.read(status(0x01, 0, 0x46, violated=(17,)))
        self.assertTrue(view.live)
        self.assertFalse(view.fire_event)
        # a second intrusion hours later, nobody disarmed: a new zone, an event
        view = self.read(status(0x01, 0, 0x46, violated=(17, 18)), dt=3600)
        self.assertTrue(view.live and view.fire_event)
        self.assertEqual(view.new_zones, {18})
        # disarmed: no longer live, memory stays in the status
        view = self.read(status(0x00, 0, 0x44, violated=(17, 18)))
        self.assertFalse(view.live or view.fire_event)

    def test_disarm_and_rearm_between_two_reads(self) -> None:
        """Contra-assinatura r4, M1: the disarm is never seen (remote control
        between polls, or Home Assistant down). Memory that clears completely
        proves the panel was armed again: the old alarm is over."""
        self.read(status(0x01))
        self.assertTrue(self.read(status(0x01, 0, 0x44, violated=(3,))).live)
        view = self.read(status(0x01))  # armed again, memory cleared
        self.assertFalse(view.live or view.fire_event)
        view = self.read(status(0x01, 0, 0x44, violated=(3,)))  # same zone fires again
        self.assertTrue(view.live and view.fire_event)
        self.assertEqual(view.alarmed, {"A"})
        # an alarm seen only through the latched bit: it dropping proves it too
        tracker = state.AlarmTracker()
        tracker.update(status(0x01), 0.0)
        self.assertTrue(tracker.update(status(0x01, 0, 0x40), 5.0).live)
        self.assertFalse(tracker.update(status(0x01), 10.0).live)

    def test_partial_clear_keeps_the_alarm(self) -> None:
        """Only a complete clear counts: one zone leaving may be another
        partition's memory being cleared."""
        self.read(status(0x03))
        self.read(status(0x03, 0, 0x40, violated=(3, 9)))
        view = self.read(status(0x03, 0, 0x40, violated=(9,)))
        self.assertEqual(view.alarmed, {"A", "B"})

    def test_latch_drop_of_another_partition_keeps_the_alarm(self) -> None:
        """Contra-assinatura r2, R2: A carries old memory (zone 3 + bit 6); B is
        broken into (zone 9). Arming A again clears zone 3 and drops bit 6: B's
        alarm is still on."""
        tracker = state.AlarmTracker()
        tracker.update(status(0x00, 0, 0x40, violated=(3,)), 0.0)  # old memory
        tracker.update(status(0x02, 0, 0x40, violated=(3,)), 5.0)  # B armed
        view = tracker.update(status(0x02, 0, 0x40, violated=(3, 9)), 10.0)
        self.assertTrue(view.fire_event and view.alarmed == {"B"})
        view = tracker.update(status(0x03, 0, 0x00, violated=(9,)), 15.0)  # A armed
        self.assertEqual(view.alarmed, {"B"})
        self.assertFalse(view.fire_event)

    def test_all_armed_partitions_show_it(self) -> None:
        self.read(status(0x03))
        view = self.read(status(0x03, violated=(5,)))
        self.assertEqual(view.alarmed, {"A", "B"})
        view = self.read(status(0x01, violated=(5,)))  # B disarmed
        self.assertEqual(view.alarmed, {"A"})

    def test_latched_bit_rising_while_armed(self) -> None:
        self.read(status(0x01))
        view = self.read(status(0x01, 0, 0x40))
        self.assertEqual(view.alarmed, {"A"})

    def test_latched_bit_rising_while_disarmed_is_an_event(self) -> None:
        """Contra-assinatura r4, M2."""
        view = self.read(status(0x00, 0, 0x40))
        self.assertTrue(view.fire_event)
        self.assertFalse(view.live)

    def test_zone_then_latch_is_one_event(self) -> None:
        """Contra-assinatura r2, R3: the zone in one read, bit 6 in the next."""
        self.read(status(0x01))
        self.assertTrue(self.read(status(0x01, violated=(3,))).fire_event)
        self.assertFalse(self.read(status(0x01, 0, 0x40, violated=(3,))).fire_event)

    def test_memory_cleared_while_the_siren_runs_is_one_event(self) -> None:
        """Contra-assinatura r2, R1: an audible 24 h zone, memory cleared while
        the siren still sounds: the same alarm, not a second event."""
        self.assertTrue(self.read(status(0x00, 0, 0x42, violated=(22,))).fire_event)
        self.assertFalse(self.read(status(0x00, 0, 0x42, violated=(22,))).fire_event)
        view = self.read(status(0x00, 0, 0x02))  # memory gone, siren on
        self.assertTrue(view.siren)
        self.assertFalse(view.fire_event)

    def test_bit2_alone_is_nothing(self) -> None:
        """Bit 2 rises with open zones (Pehesi97 #10): never an alarm alone."""
        view = self.read(status(0x01, 0, 0x04, opened=(5,)))
        self.assertFalse(view.live or view.fire_event)

    def test_memory_from_before_never_fires(self) -> None:
        tracker = state.AlarmTracker()
        memory = protocol.parse_status(FIELD_MEMORY)
        self.assertFalse(tracker.update(memory, 0.0).fire_event)  # first read
        # armed later with the memory still there (if the panel keeps it)
        armed = bytearray(FIELD_MEMORY)
        armed[27] = 0x02  # B
        view = tracker.update(protocol.parse_status(bytes(armed)), 5.0)
        self.assertFalse(view.live or view.fire_event)

    def test_new_zone_while_memory_is_latched(self) -> None:
        """The case 0.1.0 missed: bit 2 already set, a new alarm, no edge."""
        tracker = state.AlarmTracker()
        tracker.update(protocol.parse_status(FIELD_MEMORY), 0.0)
        armed = bytearray(FIELD_MEMORY)
        armed[27] = 0x02  # B armed
        tracker.update(protocol.parse_status(bytes(armed)), 5.0)
        armed[8] |= 0x10  # zone 5 violated
        view = tracker.update(protocol.parse_status(bytes(armed)), 10.0)
        self.assertTrue(view.fire_event)
        self.assertEqual((view.alarmed, view.new_zones), ({"B"}, {5}))

    def test_siren_needs_confirmation(self) -> None:
        # a confirmation beep in the strict read one second after a command
        view = self.read(status(0x01, 0, 0x02), dt=1.0)
        self.assertFalse(view.siren or view.live or view.fire_event)
        view = self.read(status(0x01), dt=4.0)  # gone at the next poll
        self.assertFalse(view.siren or view.fire_event)
        # a real siren: seen across reads >= SIREN_CONFIRM_SECONDS apart
        self.read(status(0x00, 0, 0x02))
        view = self.read(status(0x00, 0, 0x02))
        self.assertTrue(view.siren and view.live and view.fire_event)
        self.assertEqual(view.alarmed, frozenset())  # nothing armed

    def test_siren_from_byte_46(self) -> None:
        """Contra-assinatura r4, M3: the sources disagree on the siren bit, so
        byte 46 bits 2 (SDK) and 3 (upstream) count as much as general bit 1."""
        for bit in (0x04, 0x08):
            with self.subTest(bit=hex(bit)):
                tracker = state.AlarmTracker()
                tracker.update(status(), 0.0)
                data = bytearray(FIELD_STATUS)
                data[45] = bit
                s = protocol.parse_status(bytes(data))
                tracker.update(s, 5.0)
                self.assertTrue(tracker.update(s, 10.0).siren)

    def test_confirmed_siren_survives_a_reload(self) -> None:
        """Contra-assinatura r4, o1: no "disarmed" read in the middle."""
        self.read(status(0x00, 0, 0x02))
        self.assertTrue(self.read(status(0x00, 0, 0x02)).siren)
        restored = state.AlarmTracker.restore(self.tracker.snapshot())
        view = restored.update(status(0x00, 0, 0x02), self.now + 1)
        self.assertTrue(view.siren)
        self.assertFalse(view.fire_event)

    def test_two_commands_with_two_beeps_are_not_a_siren(self) -> None:
        """Arm A (the strict read 1 s later catches a beep), arm B 4 s later
        (another beep), no poll between: never a siren."""
        view = self.tracker.update(status(0x01, 0, 0x02), self.now + 1, after_command=True)
        self.assertFalse(view.siren or view.live)
        view = self.tracker.update(status(0x03, 0, 0x02), self.now + 5, after_command=True)
        self.assertFalse(view.siren or view.live or view.fire_event)
        self.now += 5
        self.assertFalse(self.read(status(0x03)).siren)

    def test_a_confirmed_siren_survives_a_command(self) -> None:
        self.read(status(0x01, 0, 0x02))
        self.assertTrue(self.read(status(0x01, 0, 0x02)).siren)
        view = self.tracker.update(status(0x00, 0, 0x02), self.now + 1, after_command=True)
        self.assertTrue(view.siren)

    def test_silent_zone_while_disarmed(self) -> None:
        view = self.read(status(violated=(22,)))
        self.assertTrue(view.fire_event)
        self.assertFalse(view.live)  # disarmed: an event, not "triggered"
        # the audible 24 h zone: the siren confirms a read later, same episode
        self.assertFalse(self.read(status(0x00, 0, 0x02, violated=(22,))).fire_event)
        view = self.read(status(0x00, 0, 0x02, violated=(22,)))
        self.assertTrue(view.live)
        self.assertFalse(view.fire_event)

    def test_quiet_read_ends_the_episode(self) -> None:
        self.read(status(0x01))
        self.assertTrue(self.read(status(0x01, violated=(3,))).fire_event)
        self.read(status(0x00, violated=(3,)))  # disarmed, memory: quiet
        self.read(status(0x01, violated=(3,)))  # armed again, memory kept
        view = self.read(status(0x01, violated=(3, 4)))
        self.assertTrue(view.fire_event)  # a second alarm, a second event

    def test_snapshot_round_trip(self) -> None:
        self.read(status(0x01))
        self.read(status(0x01, 0, 0x40, violated=(9,)))
        restored = state.AlarmTracker.restore(self.tracker.snapshot())
        view = restored.update(status(0x01, 0, 0x40, violated=(9,)), self.now + 5)
        self.assertTrue(view.live)
        self.assertFalse(view.fire_event)  # the same alarm, not a new one
        self.assertIsNone(state.AlarmTracker().snapshot())
        fresh = state.AlarmTracker.restore(None)
        self.assertFalse(fresh.initialized)


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
