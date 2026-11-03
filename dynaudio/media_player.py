"""
Support for Dynaudio Connect

Based on the API documentation found here:
https://github.com/therealmuffin/Dynaudio-connect-api
"""

from datetime import timedelta
import logging
import math
import socket
import threading

import voluptuous as vol

from homeassistant.components.media_player import MediaPlayerEntity, PLATFORM_SCHEMA
from homeassistant.components.media_player.const import MediaPlayerEntityFeature
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, STATE_OFF, STATE_ON
import homeassistant.helpers.config_validation as cv

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "Dynaudio"
DEFAULT_PORT = 1901
DEFAULT_MAX_VOLUME = 31
DEFAULT_STANDARD_ZONE = 1

# Poll less often; every poll opens a new connection and sends a command.
SCAN_INTERVAL = timedelta(seconds=60)

SOCKET_TIMEOUT = 2
LOCK_TIMEOUT = 0.25

CONF_MAX_VOLUME = "max_volume"
CONF_DEFAULT_STANDARD_ZONE = "default_zone"

SUPPORT_DYNAUDIO = (
    MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.VOLUME_MUTE
    | MediaPlayerEntityFeature.TURN_ON
    | MediaPlayerEntityFeature.TURN_OFF
    | MediaPlayerEntityFeature.SELECT_SOURCE
)

SOURCES = {
    "Minijack": 1,
    "Line": 2,
    "Optical": 3,
    "Coax": 4,
    "USB": 5,
    "Bluetooth": 6,
    "Stream": 7,
}
SOURCE_NAMES = {number: name for name, number in SOURCES.items()}


def ipv4_address(value):
    """Require a numeric address so DNS cannot block a socket operation."""
    value = cv.string(value)
    try:
        socket.inet_pton(socket.AF_INET, value)
    except OSError as exc:
        raise vol.Invalid("Use the device's IPv4 address, not a hostname") from exc
    return value


PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_HOST): ipv4_address,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
        vol.Optional(CONF_MAX_VOLUME, default=DEFAULT_MAX_VOLUME): cv.positive_int,
        vol.Optional(
            CONF_DEFAULT_STANDARD_ZONE, default=DEFAULT_STANDARD_ZONE
        ): cv.positive_int,
    }
)


def setup_platform(hass, config, add_entities, discovery_info=None):
    """Set up the Dynaudio platform."""
    add_entities(
        [
            DynaudioEntity(
                config[CONF_NAME],
                config[CONF_HOST],
                config[CONF_PORT],
                config[CONF_MAX_VOLUME],
                config[CONF_DEFAULT_STANDARD_ZONE],
            )
        ]
    )


class DynaudioEntity(MediaPlayerEntity):
    """Representation of a Dynaudio device."""

    def __init__(self, name, host, port, max_volume, standard_zone):
        self._name = name
        self._host = host
        self._port = port
        self._max_volume = min(max_volume, 31)
        self._zone = min(standard_zone, 3)

        self._pwstate = False
        self._volume = 0.0
        self._muted = False
        self._selected_source = ""
        self._available = False
        self._consecutive_connect_fails = 0
        self._lock = threading.Lock()

    @staticmethod
    def calculate_checksum(payload):
        """Calculate the checksum using the existing device protocol."""
        values = [int(value, 16) for value in payload.split()]
        total = sum(values)
        x = math.ceil(total / 255)
        checksum = x * 255 - total - (len(values) - x)
        return f"{checksum & 0xFF:02X}"

    def construct_command(self, payload):
        """Construct a complete command."""
        size = len(payload.split())
        return f"FF 55 {size:02X} {payload} {self.calculate_checksum(payload)}"

    def socket_command(self, payload, *, poll=False):
        """Send a command; only a poll response determines device status."""
        if not self._lock.acquire(timeout=LOCK_TIMEOUT):
            _LOGGER.warning(
                "%s: socket busy; skipped command rather than waiting indefinitely",
                self._name,
            )
            return False

        try:
            command = bytes.fromhex(self.construct_command(payload))

            with socket.create_connection(
                (self._host, self._port), timeout=SOCKET_TIMEOUT
            ) as sock:
                sock.settimeout(SOCKET_TIMEOUT)
                sock.sendall(command)
                received = sock.recv(1024)

            if poll:
                if len(received) < 11:
                    raise ValueError(
                        f"incomplete status response ({len(received)} bytes)"
                    )

                source = SOURCE_NAMES.get(received[8])
                if source is None:
                    raise ValueError(f"unknown source number {received[8]}")

                self._volume = min(received[7] / self._max_volume, 1.0)
                self._selected_source = source
                self._muted = bool(received[10])
                self._pwstate = bool(received[6])
                self._consecutive_connect_fails = 0
                self._available = True

            return True

        except (OSError, ValueError) as exc:
            if poll:
                self._consecutive_connect_fails += 1
                if self._consecutive_connect_fails >= 3:
                    self._available = False

            _LOGGER.debug("%s: device command failed: %s", self._name, exc)
            return False

        finally:
            self._lock.release()


    def update(self):
        """Poll using the existing unused-zone command workaround."""
        payload = "2F A0 12 00 72" if self._zone == 1 else "2F A0 12 00 71"
        self.socket_command(payload, poll=True)

    @property
    def name(self):
        return self._name

    @property
    def available(self):
        return self._available

    @property
    def state(self):
        return STATE_ON if self._pwstate else STATE_OFF

    @property
    def volume_level(self):
        return self._volume

    @property
    def is_volume_muted(self):
        return self._muted

    @property
    def supported_features(self):
        return SUPPORT_DYNAUDIO

    @property
    def source(self):
        return self._selected_source

    @property
    def source_list(self):
        return list(SOURCES)

    @property
    def media_title(self):
        return self._selected_source if self._pwstate else "Off"

    def turn_off(self):
        self.socket_command(f"2F A0 02 01 F{self._zone}")

    def turn_on(self):
        self.socket_command(f"2F A0 01 00 F{self._zone}")

    def set_volume_level(self, volume):
        current_source = SOURCES.get(self._selected_source)
        if current_source is None:
            _LOGGER.warning(
                "%s: cannot set volume until a valid source has been read",
                self._name,
            )
            return

        level = round(max(0.0, min(volume, 1.0)) * self._max_volume)
        self.socket_command(
            f"2F A0 13 {level:02X} {current_source}{self._zone}"
        )

    def mute_volume(self, mute):
        # The existing device command toggles mute; it does not set it.
        if mute != self._muted:
            self.socket_command(f"2F A0 12 01 3{self._zone}")

    def select_source(self, source):
        requested_source = SOURCES.get(source)
        current_source = SOURCES.get(self._selected_source)
        if requested_source is None or current_source is None:
            _LOGGER.warning(
                "%s: cannot select source without a valid requested and "
                "current source",
                self._name,
            )
            return

        self.socket_command(
            f"2F A0 15 {requested_source:02X} "
            f"{current_source}{self._zone}"
        )
