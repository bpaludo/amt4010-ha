"""ISECMobile (0xE9) protocol of the Intelbras AMT 4010 — pure, no Home Assistant.

Every fact below cites its source. "SDK" is the Intelbras spreadsheet
"SDKCentraisDeAlarmeIntelbras-v1.0.1", sheet "4-Comandar central via APP"
(row numbers refer to that sheet). "Field" is a capture from a real AMT 4010,
firmware 6.6, on 2026-09-29. Where the SDK and the field disagree, the field
wins and the disagreement is written down next to the code.

Frame sent to the panel:

    [len] E9 21 <password ASCII, 4 or 6 digits> <command> [content] 21 [checksum]

``len`` counts from E9 up to the closing 21; the checksum is the XOR of every
byte from ``len`` on, inverted. The password travels in clear in every frame:
nothing in this module logs a frame.
"""
from __future__ import annotations

import dataclasses
import datetime as dt

ISEC_MOBILE = 0xE9
DELIMITER = 0x21
ACK = 0xFE

CMD_PARTIAL_STATUS = 0x5A  # SDK row 118; the AMT 4010 answers NACK E5 (field)
CMD_STATUS = 0x5B  # SDK row 291
CMD_READ_EEPROM = 0x5C  # SDK row 780; AMT 4010 Smart >= 3.20
CMD_ARM = 0x41  # SDK row 828
CMD_DISARM = 0x44  # SDK row 920

# Partition byte of the arm/disarm commands (SDK rows 841-845).
PARTITIONS: dict[str, int] = {"A": 0x41, "B": 0x42, "C": 0x43, "D": 0x44}

STATUS_LENGTH = 54  # SDK row 296 ("54 Bytes")
MODEL_AMT4010 = 0x41  # SDK row 345; field
# Stay per partition is reported from this firmware on (upstream
# andregoncalvespires/intelbras_alarm; not in the SDK).
STAY_STATUS_MIN_FIRMWARE = (5, 7)

ZONE_COUNT = 64
# Zone names live in EEPROM, 16-byte records from 0x0800, zone 1 first
# (andregoncalvespires/intelbras_alarm, "confirmed by real capture"; not in the
# SDK). 64 zones end at 0x0C00.
ZONE_NAMES_START = 0x0800
ZONE_NAMES_END = 0x0C00
ZONE_NAME_RECORD = 16
MAX_EEPROM_READ = 0xC0  # 12 names per read (same source)

MODELS = {
    0x1E: "AMT 2018 E/EG",
    0x20: "AMT 2110",
    0x24: "ANM 24 NET",
    0x2E: "AMT 2118 EG",
    0x32: "AMT 2018 E3G",
    0x34: "AMT 2018 E Smart",
    0x35: "ELC 3020 NET",
    0x41: "AMT 4010",
    0x61: "AMT 1016 NET",
}  # SDK rows 344-352

# NACK codes, SDK rows 54-112.
NACK_E1_PASSWORD = 0xE1
NACK_E5_DISCONTINUED = 0xE5
NACK_MESSAGES = {
    0xE0: "frame format not recognized",
    0xE1: "wrong password",
    0xE2: "invalid command",
    0xE3: "panel is not partitioned",
    0xE4: "zones are open",
    0xE5: "command discontinued",
    0xE6: "user may not bypass zones",
    0xE7: "user may not disarm",
    0xE8: "bypass not allowed while armed",
    0xEA: "partition has no enabled zones",
}


class NotAllowed(ValueError):
    """The frame is outside the allowlist. Raised before any connection."""


class ProtocolError(ValueError):
    """A reply that cannot be trusted: size, checksum or header."""


def checksum(data: bytes) -> int:
    value = 0
    for byte in data:
        value ^= byte
    return value ^ 0xFF


def valid_password(password: str) -> bool:
    """4 or 6 ASCII digits (SDK row 23: "4 ou seis dígitos")."""
    return len(password) in (4, 6) and password.isascii() and password.isdigit()


def build_frame(password: str, command: int, content: bytes = b"") -> bytes:
    """Build a frame. Knows nothing about what may be sent: see check_allowed."""
    if not valid_password(password):
        raise ValueError("the password must have 4 or 6 digits")
    body = (
        bytes((ISEC_MOBILE, DELIMITER))
        + password.encode("ascii")
        + bytes((command,))
        + content
        + bytes((DELIMITER,))
    )
    frame = bytes((len(body),)) + body
    return frame + bytes((checksum(frame),))


def eeprom_read_content(address: int, quantity: int) -> bytes:
    return bytes((address >> 8, address & 0xFF, quantity))


def check_allowed(command: int, content: bytes, commands_enabled: bool) -> None:
    """The allowlist. The client calls it before opening any connection.

    Reads: 5A and 5B without content; 5C only inside the zone-name area.
    Commands: 41 (arm) and 44 (disarm), whole panel or one partition, never
    stay, and only when commands are enabled. Everything else is refused.
    """
    if command in (CMD_PARTIAL_STATUS, CMD_STATUS):
        if content:
            raise NotAllowed(f"0x{command:02X} takes no content")
        return
    if command == CMD_READ_EEPROM:
        if len(content) != 3:
            raise NotAllowed("0x5C takes address (2 bytes) and quantity (1 byte)")
        address = (content[0] << 8) | content[1]
        quantity = content[2]
        if not (
            1 <= quantity <= MAX_EEPROM_READ
            and ZONE_NAMES_START <= address
            and address + quantity <= ZONE_NAMES_END
        ):
            raise NotAllowed("0x5C is limited to the zone-name area")
        return
    if command in (CMD_ARM, CMD_DISARM):
        if not commands_enabled:
            raise NotAllowed("arm/disarm is disabled")
        if content == b"" or (len(content) == 1 and content[0] in PARTITIONS.values()):
            return
        raise NotAllowed(f"0x{command:02X} takes nothing or one partition byte")
    raise NotAllowed(f"0x{command:02X} is not allowed")


def parse_reply(raw: bytes) -> bytes:
    """Validate a whole reply and return its data (after E9, before the checksum)."""
    if len(raw) < 4:
        raise ProtocolError(f"reply too short ({len(raw)} bytes)")
    if raw[0] < 2 or len(raw) != raw[0] + 2:
        raise ProtocolError(f"reply size {len(raw)} does not match its header")
    if checksum(raw[:-1]) != raw[-1]:
        raise ProtocolError("reply checksum does not match")
    if raw[1] != ISEC_MOBILE:
        raise ProtocolError(f"unexpected reply command 0x{raw[1]:02X}")
    return raw[2:-1]


def nack_code(data: bytes) -> int | None:
    """None for ACK. Raises ProtocolError for a one-byte reply that is neither."""
    if len(data) != 1:
        raise ProtocolError(f"expected ACK/NACK, got {len(data)} bytes")
    if data[0] == ACK:
        return None
    if data[0] in NACK_MESSAGES:
        return data[0]
    raise ProtocolError(f"unknown one-byte reply 0x{data[0]:02X}")


# ---------------------------------------------------------------------------
# Status 0x5B (SDK rows 306-471). Offsets below are 0-based; the SDK counts
# bytes from 1, so SDK "Byte 25" is offset 24.
# ---------------------------------------------------------------------------
OFF_OPEN = 0  # SDK bytes 1-8, row 317
OFF_VIOLATED = 8  # SDK bytes 9-16, row 337
OFF_BYPASSED = 16  # SDK bytes 17-24, row 340
OFF_MODEL = 24  # SDK byte 25, row 343
OFF_FIRMWARE = 25  # SDK byte 26, row 353 (one digit per nibble)
OFF_PARTITIONED = 26  # SDK byte 27, row 354
OFF_PARTITIONS_AB = 27  # SDK byte 28, rows 357-361: bit 0 = A, bit 1 = B
OFF_PARTITIONS_CD = 28  # SDK byte 29, rows 362-366: bit 0 = C, bit 1 = D
OFF_GENERAL = 29  # SDK byte 30, rows 368-373
OFF_CLOCK = 30  # SDK bytes 31-35: hour, minute, day, month, year
OFF_POWER = 35  # SDK byte 36, rows 385-390
OFF_BUS = 36  # SDK bytes 37-40, rows 392-405
OFF_BATTERY_ICON = 40  # SDK byte 41, rows 408-414
OFF_KEYPAD_TAMPER = 41  # SDK byte 42, rows 416-421
OFF_SYSTEM = 42  # SDK byte 43, rows 423-428
OFF_ZONE_TAMPER = 43  # SDK byte 44, rows 430-434 (zones 1-8)
OFF_ZONE_SHORT = 44  # SDK byte 45, rows 436-440 (zones 1-8)
OFF_SIREN_PGM = 45  # SDK byte 46, rows 442-447
OFF_LOW_BATTERY = 46  # SDK bytes 47-52, rows 449-463 (zones 17-64)
OFF_PGM_EXPANDER = 52  # SDK bytes 53-54, rows 465-471

# General byte (SDK row 370): bit 0 problems, bit 1 siren on, bit 2 zones
# firing, bit 3 panel armed. Neither bit 2 nor bit 6 says "alarm now":
# - bit 2 stayed set for more than a day after an alarm, the panel disarmed and
#   no zone open, while a zone stayed in the violated map (field, fw 6.6,
#   2026-10: general 0x44); Pehesi97 issue #10 saw it rise with open
#   zones on another AMT 4010; andregoncalvespires reads it as "some zone open";
# - bit 6 (not in the SDK) is the trigger latched until the partition is armed
#   again (andregoncalvespires, field captures); set in the same capture.
# So the alarm state is derived in state.AlarmTracker, never from one bit.
GENERAL_PROBLEM = 0x01
GENERAL_SIREN = 0x02
GENERAL_FIRING = 0x04
GENERAL_ARMED = 0x08
GENERAL_LATCHED_UPSTREAM = 0x40

# Power byte (SDK row 387).
POWER_AC_FAILURE = 0x01
POWER_BATTERY_LOW = 0x02
POWER_BATTERY_MISSING = 0x04  # "ausente ou invertida"
POWER_BATTERY_SHORT = 0x08
POWER_AUX_OVERLOAD = 0x10

# System problems byte (SDK row 425).
SYSTEM_SIREN_WIRE_CUT = 0x01
SYSTEM_SIREN_SHORT = 0x02
SYSTEM_PHONE_LINE_CUT = 0x04
SYSTEM_EVENT_COMM_FAILURE = 0x08

# Siren/PGM byte (SDK row 444: siren = bit 2). Upstream andregoncalvespires
# says the AMT 4010 uses bit 3 ("confirmed by a user"), the SDK says bit 2 and
# marks bit 3 N/A. The siren state comes from the general byte (bit 1), where
# the SDK and Pehesi97 agree; both raw bits are kept as attributes so a real
# alarm settles the question.
SIREN_PGM_SIREN_SDK = 0x04
SIREN_PGM_SIREN_UPSTREAM = 0x08


def _bitmap(data: bytes, start: int, count: int, first_zone: int = 1) -> frozenset[int]:
    """Bit j of byte i is zone first_zone + 8*i + j (SDK rows 320-335)."""
    zones = set()
    for i in range(count):
        byte = data[start + i]
        for bit in range(8):
            if byte >> bit & 1:
                zones.add(first_zone + 8 * i + bit)
    return frozenset(zones)


def _bits(byte: int, labels: tuple[int, ...], shift: int = 0) -> tuple[int, ...]:
    return tuple(label for i, label in enumerate(labels) if byte >> (shift + i) & 1)


def _clock(data: bytes) -> dt.datetime | None:
    """Panel clock, local time of the panel, minute precision.

    The SDK (rows 375-384) says BCD. The field says binary: the capture of
    2026-09-29 16:58 carries minute 0x3A, which is not BCD at all, and
    ``10 3a 1d 09 1a`` reads 16:58 29/09/26 only as plain binary.
    """
    hour, minute, day, month, year = data[OFF_CLOCK : OFF_CLOCK + 5]
    try:
        return dt.datetime(2000 + year, month, day, hour, minute)
    except ValueError:
        return None


@dataclasses.dataclass(frozen=True)
class Status:
    raw: bytes
    model: int
    firmware: tuple[int, int]
    partitioned: bool
    partitions_armed: dict[str, bool]
    # None when the firmware does not report it.
    partitions_stay: dict[str, bool] | None
    general: int
    clock: dt.datetime | None
    power: int
    bus: bytes
    battery_icon: int
    keypad_tamper_byte: int
    system: int
    siren_pgm: int
    # PGM outputs that are on: 1-3 from the siren/PGM byte, 4-19 from the
    # expander bytes (SDK rows 444-447 and 465-471). Diagnostics only.
    pgm_on: frozenset[int]
    open_zones: frozenset[int]
    violated_zones: frozenset[int]
    bypassed_zones: frozenset[int]
    tamper_zones: frozenset[int]
    short_zones: frozenset[int]
    low_battery_zones: frozenset[int]

    @property
    def model_name(self) -> str:
        return MODELS.get(self.model, f"0x{self.model:02X}")

    @property
    def firmware_version(self) -> str:
        return f"{self.firmware[0]}.{self.firmware[1]}"

    @property
    def siren_on(self) -> bool:
        return bool(self.general & GENERAL_SIREN)

    @property
    def siren_any(self) -> bool:
        """Any of the three bits the sources give for the siren: general bit 1
        (SDK, Pehesi97), byte 46 bit 2 (SDK), byte 46 bit 3 (andregoncalvespires,
        "confirmed by a user" for the 4010). None is proven on this panel yet, and
        missing a siren is worse than a contested bit: all three count."""
        return self.siren_on or bool(
            self.siren_pgm & (SIREN_PGM_SIREN_SDK | SIREN_PGM_SIREN_UPSTREAM)
        )

    @property
    def zones_firing(self) -> bool:
        """General bit 2, the SDK's "zones firing". Raw only: in the field it
        stays set with alarm memory (see GENERAL_FIRING)."""
        return bool(self.general & GENERAL_FIRING)

    @property
    def trigger_latched(self) -> bool:
        """General bit 6: trigger latched until re-armed (upstream). Raw."""
        return bool(self.general & GENERAL_LATCHED_UPSTREAM)

    @property
    def alarm_memory(self) -> bool:
        """The panel remembers an alarm: a violated zone or the latched bit."""
        return bool(self.violated_zones) or self.trigger_latched

    @property
    def armed_flag(self) -> bool:
        return bool(self.general & GENERAL_ARMED)

    @property
    def problem_flag(self) -> bool:
        return bool(self.general & GENERAL_PROBLEM)

    @property
    def ac_failure(self) -> bool:
        return bool(self.power & POWER_AC_FAILURE)

    @property
    def aux_overload(self) -> bool:
        return bool(self.power & POWER_AUX_OVERLOAD)

    @property
    def battery_problem(self) -> bool:
        return bool(
            self.power & (POWER_BATTERY_LOW | POWER_BATTERY_MISSING | POWER_BATTERY_SHORT)
        )

    def battery_details(self) -> dict[str, object]:
        icon = self.battery_icon
        return {
            "low": bool(self.power & POWER_BATTERY_LOW),
            "missing_or_reversed": bool(self.power & POWER_BATTERY_MISSING),
            "short_circuit": bool(self.power & POWER_BATTERY_SHORT),
            # Keypad battery icon (SDK row 409): low nibble on/off, high blinking.
            "icon_outline": bool(icon & 0x01),
            "icon_bars": sum(1 for bit in (1, 2, 3) if icon >> bit & 1),
            "icon_blinking": bool(icon & 0xF0),
        }

    @property
    def siren_wiring_problem(self) -> bool:
        return bool(self.system & (SYSTEM_SIREN_WIRE_CUT | SYSTEM_SIREN_SHORT))

    @property
    def phone_line_cut(self) -> bool:
        return bool(self.system & SYSTEM_PHONE_LINE_CUT)

    @property
    def event_comm_failure(self) -> bool:
        return bool(self.system & SYSTEM_EVENT_COMM_FAILURE)

    def bus_problems(self) -> dict[str, tuple[int, ...]]:
        """SDK rows 395-405: keypads/receivers, expanders."""
        return {
            "keypads": _bits(self.bus[0], (1, 2, 3, 4)),
            "receivers": _bits(self.bus[0], (1, 2, 3, 4), shift=4),
            "pgm_expanders": _bits(self.bus[1], (1, 2, 3, 4)),
            "zone_expanders": _bits(self.bus[1], (1, 2, 3, 4), shift=4)
            + _bits(self.bus[2], (5, 6)),
        }

    @property
    def bus_problem(self) -> bool:
        return any(self.bus_problems().values())

    @property
    def keypads_tampered(self) -> tuple[int, ...]:
        """SDK row 418: bits 4-7 = keypads 1-4."""
        return _bits(self.keypad_tamper_byte, (1, 2, 3, 4), shift=4)

    def partition_state_bits(self) -> dict[str, object]:
        return {
            "byte_27": f"0x{self.raw[OFF_PARTITIONS_AB]:02x}",
            "byte_28": f"0x{self.raw[OFF_PARTITIONS_CD]:02x}",
        }

    def siren_bits(self) -> dict[str, bool]:
        return {
            "general_bit1": self.siren_on,
            "siren_pgm_bit2_sdk": bool(self.siren_pgm & SIREN_PGM_SIREN_SDK),
            "siren_pgm_bit3_upstream": bool(self.siren_pgm & SIREN_PGM_SIREN_UPSTREAM),
        }


def parse_status(data: bytes) -> Status:
    """Decode the 54 status bytes of 0x5B. Any other size is refused."""
    if len(data) != STATUS_LENGTH:
        raise ProtocolError(f"status has {len(data)} bytes, expected {STATUS_LENGTH}")
    if data[OFF_PARTITIONED] not in (0x00, 0x01):
        # SDK row 354: 0x00 or 0x01. Anything else means we do not understand
        # the panel; better unavailable than a guessed partition layout.
        raise ProtocolError(f"partitioning byte 0x{data[OFF_PARTITIONED]:02X} is undocumented")
    firmware = (data[OFF_FIRMWARE] >> 4, data[OFF_FIRMWARE] & 0x0F)
    ab, cd = data[OFF_PARTITIONS_AB], data[OFF_PARTITIONS_CD]
    armed = {"A": bool(ab & 1), "B": bool(ab & 2), "C": bool(cd & 1), "D": bool(cd & 2)}
    stay = None
    if data[OFF_MODEL] == MODEL_AMT4010 and firmware >= STAY_STATUS_MIN_FIRMWARE:
        # Bits 4/5 of the partition bytes (upstream andregoncalvespires).
        stay = {
            "A": bool(ab & 0x10),
            "B": bool(ab & 0x20),
            "C": bool(cd & 0x10),
            "D": bool(cd & 0x20),
        }
    return Status(
        raw=bytes(data),
        model=data[OFF_MODEL],
        firmware=firmware,
        partitioned=data[OFF_PARTITIONED] == 0x01,
        partitions_armed=armed,
        partitions_stay=stay,
        general=data[OFF_GENERAL],
        clock=_clock(data),
        power=data[OFF_POWER],
        bus=bytes(data[OFF_BUS : OFF_BUS + 4]),
        battery_icon=data[OFF_BATTERY_ICON],
        keypad_tamper_byte=data[OFF_KEYPAD_TAMPER],
        system=data[OFF_SYSTEM],
        siren_pgm=data[OFF_SIREN_PGM],
        pgm_on=frozenset(
            _bits(data[OFF_SIREN_PGM], (3, 2, 1), shift=4)
            + _bits(data[OFF_PGM_EXPANDER], tuple(range(4, 12)))
            + _bits(data[OFF_PGM_EXPANDER + 1], tuple(range(12, 20)))
        ),
        open_zones=_bitmap(data, OFF_OPEN, 8),
        violated_zones=_bitmap(data, OFF_VIOLATED, 8),
        bypassed_zones=_bitmap(data, OFF_BYPASSED, 8),
        tamper_zones=_bitmap(data, OFF_ZONE_TAMPER, 1),
        short_zones=_bitmap(data, OFF_ZONE_SHORT, 1),
        low_battery_zones=_bitmap(data, OFF_LOW_BATTERY, 6, first_zone=17),
    )


# ---------------------------------------------------------------------------
# Zone names (EEPROM via 0x5C)
# ---------------------------------------------------------------------------
def zone_name_blocks() -> list[tuple[int, int]]:
    """(address, quantity) of the 6 reads that cover the 64 names."""
    blocks = []
    address = ZONE_NAMES_START
    while address < ZONE_NAMES_END:
        quantity = min(MAX_EEPROM_READ, ZONE_NAMES_END - address)
        blocks.append((address, quantity))
        address += quantity
    return blocks


def _factory_pattern(raw: bytes, zone: int) -> bool:
    """Names nobody programmed.

    - "Zona NN" with the zone's own number: the default of firmware 6.6 (field,
      2026-09-29: 45 of 64 zones read back exactly like that);
    - consecutive ASCII such as "ABCDEFGHIJKLMN" (upstream andregoncalvespires,
      "confirmed by real capture", other firmwares).
    """
    text = raw.decode("latin-1").strip().casefold()
    if text.startswith("zona") and text[4:].strip().lstrip("0") == str(zone):
        return True
    # Only long runs: "01", "12" or "23" are names an installer really types.
    return len(raw) >= 8 and all(raw[i] + 1 == raw[i + 1] for i in range(len(raw) - 1))


def decode_zone_names(address: int, eeprom: bytes) -> dict[int, str | None]:
    """16-byte records, NUL-terminated. None = no name programmed."""
    names: dict[int, str | None] = {}
    first = (address - ZONE_NAMES_START) // ZONE_NAME_RECORD + 1
    for i in range(len(eeprom) // ZONE_NAME_RECORD):
        record = eeprom[i * ZONE_NAME_RECORD : (i + 1) * ZONE_NAME_RECORD]
        # NUL-terminated; 0xFF is unwritten EEPROM, also used as padding.
        raw = record.split(b"\x00", 1)[0].rstrip(b"\xff")
        text = "".join(c for c in raw.decode("latin-1") if c.isprintable()).strip()
        if not text or _factory_pattern(raw, first + i):
            names[first + i] = None
        else:
            names[first + i] = text
    return names


def parse_eeprom_reply(data: bytes, quantity: int) -> bytes:
    """SDK rows 798-802: user index, then the bytes read."""
    if len(data) != quantity + 1:
        raise ProtocolError(f"EEPROM reply has {len(data) - 1} bytes, asked {quantity}")
    return data[1:]
