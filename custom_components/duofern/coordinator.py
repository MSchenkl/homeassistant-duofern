"""DataUpdateCoordinator for DuoFern integration.

Push-based coordinator — no polling. State is updated when the stick
receives messages from devices. async_set_updated_data() pushes new
state to all subscribed entities.

The coordinator owns the DuoFernStick instance and is the single point
of truth for all device states. It also manages:
  - Pairing / unpairing mode with 60s auto-stop timer
  - Error handling: MISSING ACK, NOT INITIALIZED
  - Channel expansion: 43ABCD -> 43ABCD01, 43ABCD02
  - Sensor event dispatch -> HA events
  - Battery status tracking
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    DOMAIN,
    PAIR_TIMEOUT,
    STATUS_RETRY_COUNT,
)
from .protocol import (
    CoverCommand,
    DuoFernDecoder,
    DuoFernEncoder,
    DuoFernId,
    ParsedStatus,
    SensorEvent,
    SwitchCommand,
    frame_to_hex,
)
from .stick import DuoFernStick

_LOGGER = logging.getLogger(__name__)

# HA event fired when a sensor / button message is received
DUOFERN_EVENT = f"{DOMAIN}_event"


# ---------------------------------------------------------------------------
# State dataclasses
# ---------------------------------------------------------------------------


@dataclass
class DuoFernDeviceState:
    """State for a single DuoFern device or channel.

    device_code holds the base 6-digit code.
    channel holds the 2-digit channel suffix (e.g. "01") or None.
    """

    device_code: DuoFernId
    channel: str | None = None
    status: ParsedStatus = field(default_factory=ParsedStatus)
    available: bool = True
    last_seen: float | None = None
    battery_state: str | None = None  # "ok" | "low" | None
    battery_percent: int | None = None


@dataclass
class DuoFernData:
    """Top-level data container pushed to all entities on every update."""

    # Key: full_hex (6-char for single-channel, 8-char for channel devices)
    devices: dict[str, DuoFernDeviceState] = field(default_factory=dict)

    # Pairing state (shown by button entities and sensors)
    pairing_active: bool = False
    unpairing_active: bool = False
    pairing_remaining: int = 0  # seconds remaining in pairing window

    # Last newly paired / unpaired device (for notifications)
    last_paired: str | None = None
    last_unpaired: str | None = None


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


class DuoFernCoordinator(DataUpdateCoordinator[DuoFernData]):
    """Coordinator that manages the DuoFern stick and all device states.

    Push-based (no polling). All state changes come from the serial protocol
    and are pushed to entities via async_set_updated_data().
    """

    def __init__(
        self,
        hass: HomeAssistant,
        port: str,
        system_code: DuoFernId,
        paired_devices: list[DuoFernId],
    ) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN)

        self._port = port
        self._system_code = system_code
        self._paired_devices = paired_devices

        # Build initial device state dict, expanding channels where needed
        self._data = DuoFernData()
        for device in paired_devices:
            self._register_device(device)

        self.data = self._data
        self._stick: DuoFernStick | None = None

        # Pairing timer handle
        self._pair_timer: asyncio.TimerHandle | None = None
        self._pair_countdown_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def system_code(self) -> DuoFernId:
        return self._system_code

    @property
    def stick(self) -> DuoFernStick | None:
        return self._stick

    @property
    def pairing_active(self) -> bool:
        return self._data.pairing_active

    @property
    def unpairing_active(self) -> bool:
        return self._data.unpairing_active

    # ------------------------------------------------------------------
    # Device registration (including channel expansion)
    # ------------------------------------------------------------------

    def _register_device(self, device: DuoFernId) -> None:
        """Register a device and its channels in the state dict.

        If the device type has sub-channels (e.g. Universalaktor with
        channels 01 and 02), individual channel entries are created instead
        of (or in addition to) the base device entry.
        """
        if device.has_channels:
            for ch in device.channel_list:
                ch_id = device.with_channel(ch)
                key = ch_id.full_hex
                if key not in self._data.devices:
                    self._data.devices[key] = DuoFernDeviceState(
                        device_code=device,
                        channel=ch,
                    )
                    _LOGGER.debug("Registered channel device %s", key)
        else:
            key = device.hex
            if key not in self._data.devices:
                self._data.devices[key] = DuoFernDeviceState(
                    device_code=device,
                )
                _LOGGER.debug("Registered device %s", key)

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open serial port and run init handshake."""
        self._stick = DuoFernStick(
            port=self._port,
            system_code=self._system_code,
            paired_devices=self._paired_devices,
            message_callback=self._on_message,
        )
        await self._stick.connect()
        _LOGGER.info("DuoFern coordinator connected (system=%s)", self._system_code.hex)

    async def disconnect(self) -> None:
        """Disconnect stick and cancel any running timers."""
        self._cancel_pair_timer()
        if self._pair_countdown_task and not self._pair_countdown_task.done():
            self._pair_countdown_task.cancel()

        if self._stick:
            await self._stick.disconnect()
            self._stick = None
        _LOGGER.info("DuoFern coordinator disconnected")

    async def _async_update_data(self) -> DuoFernData:
        """Required by base class — not used for push-based coordinator."""
        return self._data

    # ------------------------------------------------------------------
    # Cover commands
    # ------------------------------------------------------------------

    async def async_cover_up(self, device_code: DuoFernId) -> None:
        frame = DuoFernEncoder.build_cover_command(
            CoverCommand.UP, device_code, self._system_code
        )
        await self._send(frame)
        self._optimistic_moving(device_code, "up")

    async def async_cover_down(self, device_code: DuoFernId) -> None:
        frame = DuoFernEncoder.build_cover_command(
            CoverCommand.DOWN, device_code, self._system_code
        )
        await self._send(frame)
        self._optimistic_moving(device_code, "down")

    async def async_cover_stop(self, device_code: DuoFernId) -> None:
        frame = DuoFernEncoder.build_cover_command(
            CoverCommand.STOP, device_code, self._system_code
        )
        await self._send(frame)
        self._optimistic_moving(device_code, "stop")

    async def async_cover_position(self, device_code: DuoFernId, position: int) -> None:
        """Send POSITION command. position: 0=open, 100=closed (DuoFern native)."""
        frame = DuoFernEncoder.build_cover_command(
            CoverCommand.POSITION,
            device_code,
            self._system_code,
            position=position,
        )
        await self._send(frame)

        # Optimistic direction from current position
        state = self._find_state(device_code)
        if state and state.status.position is not None:
            if position > state.status.position:
                self._optimistic_moving(device_code, "down")
            elif position < state.status.position:
                self._optimistic_moving(device_code, "up")

    # ------------------------------------------------------------------
    # Switch / dimmer commands
    # ------------------------------------------------------------------

    async def async_switch_on(self, device_code: DuoFernId, channel: int = 1) -> None:
        frame = DuoFernEncoder.build_switch_command(
            SwitchCommand.ON, device_code, self._system_code, channel=channel
        )
        await self._send(frame)

    async def async_switch_off(self, device_code: DuoFernId, channel: int = 1) -> None:
        frame = DuoFernEncoder.build_switch_command(
            SwitchCommand.OFF, device_code, self._system_code, channel=channel
        )
        await self._send(frame)

    async def async_set_level(
        self, device_code: DuoFernId, level: int, channel: int = 1
    ) -> None:
        """Set dimmer level 0-100."""
        frame = DuoFernEncoder.build_dim_command(
            level, device_code, self._system_code, channel=channel
        )
        await self._send(frame)

    # ------------------------------------------------------------------
    # Status request
    # ------------------------------------------------------------------

    async def async_request_status(self, device_code: DuoFernId | None = None) -> None:
        """Request status from one device or broadcast to all."""
        if device_code is None:
            frame = DuoFernEncoder.build_status_request_broadcast()
        else:
            frame = DuoFernEncoder.build_status_request(device_code, self._system_code)
        await self._send(frame)

    # ------------------------------------------------------------------
    # Pairing
    # ------------------------------------------------------------------

    async def async_start_pairing(self) -> None:
        """Enter pairing mode for PAIR_TIMEOUT seconds, then auto-stop."""
        if self._data.pairing_active or self._data.unpairing_active:
            _LOGGER.warning("Pair/unpair already active, ignoring")
            return

        _LOGGER.info("Starting pairing mode (%ds)", PAIR_TIMEOUT)
        await self._send(DuoFernEncoder.build_start_pair())

        self._data.pairing_active = True
        self._data.pairing_remaining = int(PAIR_TIMEOUT)
        self.async_set_updated_data(self._data)

        self._pair_countdown_task = self.hass.async_create_task(
            self._pairing_countdown(pairing=True)
        )

    async def async_stop_pairing(self) -> None:
        """Manually stop pairing mode."""
        if not self._data.pairing_active:
            return
        await self._end_pairing(pairing=True)

    async def async_start_unpairing(self) -> None:
        """Enter unpairing mode for PAIR_TIMEOUT seconds, then auto-stop."""
        if self._data.pairing_active or self._data.unpairing_active:
            _LOGGER.warning("Pair/unpair already active, ignoring")
            return

        _LOGGER.info("Starting unpairing mode (%ds)", PAIR_TIMEOUT)
        await self._send(DuoFernEncoder.build_start_unpair())

        self._data.unpairing_active = True
        self._data.pairing_remaining = int(PAIR_TIMEOUT)
        self.async_set_updated_data(self._data)

        self._pair_countdown_task = self.hass.async_create_task(
            self._pairing_countdown(pairing=False)
        )

    async def async_stop_unpairing(self) -> None:
        """Manually stop unpairing mode."""
        if not self._data.unpairing_active:
            return
        await self._end_pairing(pairing=False)

    async def _pairing_countdown(self, pairing: bool) -> None:
        """Count down PAIR_TIMEOUT seconds, updating remaining time every second."""
        try:
            for remaining in range(int(PAIR_TIMEOUT), 0, -1):
                self._data.pairing_remaining = remaining
                self.async_set_updated_data(self._data)
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            return
        # Timer expired — auto-stop
        await self._end_pairing(pairing=pairing)

    async def _end_pairing(self, pairing: bool) -> None:
        """Send stop command and clear pairing state."""
        if self._pair_countdown_task and not self._pair_countdown_task.done():
            self._pair_countdown_task.cancel()
            self._pair_countdown_task = None

        if pairing:
            await self._send(DuoFernEncoder.build_stop_pair())
            self._data.pairing_active = False
            _LOGGER.info("Pairing mode ended")
        else:
            await self._send(DuoFernEncoder.build_stop_unpair())
            self._data.unpairing_active = False
            _LOGGER.info("Unpairing mode ended")

        self._data.pairing_remaining = 0
        self.async_set_updated_data(self._data)

    def _cancel_pair_timer(self) -> None:
        if self._pair_timer:
            self._pair_timer.cancel()
            self._pair_timer = None

    # ------------------------------------------------------------------
    # Incoming message handler
    # ------------------------------------------------------------------

    @callback
    def _on_message(self, frame: bytearray) -> None:
        """Dispatch an incoming frame from the stick.

        Called in the event loop from the serial protocol.
        """
        try:
            self._dispatch(frame)
        except Exception:
            _LOGGER.exception("Error handling frame: %s", frame_to_hex(frame))

    def _dispatch(self, frame: bytearray) -> None:
        """Route a frame to the appropriate handler."""

        # --- Error / protocol frames ---
        if DuoFernDecoder.is_not_initialized(frame):
            _LOGGER.error("DuoFern stick NOT INITIALIZED (81010C55) — will reconnect")
            self.hass.async_create_task(self._reconnect())
            return

        if DuoFernDecoder.is_missing_ack(frame):
            device_code = DuoFernDecoder.extract_device_code(frame)
            _LOGGER.warning("MISSING ACK (810108AA) for device %s", device_code.hex)
            state = self._find_state(device_code)
            if state:
                state.available = False
                self.async_set_updated_data(self._data)
            return

        if DuoFernDecoder.is_cmd_ack(frame):
            # Command acknowledged by actor — request fresh status
            device_code = DuoFernDecoder.extract_device_code(frame)
            _LOGGER.debug("Cmd ACK from %s, requesting status", device_code.hex)
            self.hass.async_create_task(self.async_request_status(device_code))
            return

        # --- Pair / unpair responses ---
        if DuoFernDecoder.is_pair_response(frame):
            device_code = DuoFernDecoder.extract_device_code(frame)
            _LOGGER.info(
                "Device paired: %s (%s)", device_code.hex, device_code.device_type_name
            )
            self._data.last_paired = device_code.hex
            self.async_set_updated_data(self._data)
            return

        if DuoFernDecoder.is_unpair_response(frame):
            device_code = DuoFernDecoder.extract_device_code(frame)
            _LOGGER.info(
                "Device unpaired: %s (%s)",
                device_code.hex,
                device_code.device_type_name,
            )
            self._data.last_unpaired = device_code.hex
            self.async_set_updated_data(self._data)
            return

        # --- Battery status ---
        if DuoFernDecoder.is_battery_status(frame):
            device_code = DuoFernDecoder.extract_device_code(frame)
            bat = DuoFernDecoder.parse_battery_status(frame)
            state = self._find_state(device_code)
            if state:
                state.battery_state = str(bat["batteryState"])
                state.battery_percent = int(bat["batteryPercent"])  # type: ignore[arg-type]
                self.async_set_updated_data(self._data)
            return

        # --- Weather data (Umweltsensor) ---
        if DuoFernDecoder.is_weather_data(frame):
            device_code = DuoFernDecoder.extract_device_code(frame)
            weather = DuoFernDecoder.parse_weather_data(frame)
            state = self._find_state(device_code)
            if state:
                state.status.readings.update(
                    {
                        "brightness": weather.brightness,
                        "sunDirection": weather.sun_direction,
                        "sunHeight": weather.sun_height,
                        "temperature": weather.temperature,
                        "isRaining": weather.is_raining,
                        "wind": weather.wind,
                    }
                )
                state.last_seen = time.time()
                self.async_set_updated_data(self._data)
            return

        # --- Sensor / button events ---
        if DuoFernDecoder.is_sensor_message(frame):
            ev = DuoFernDecoder.parse_sensor_event(frame)
            if ev:
                self._fire_sensor_event(ev)
            return

        # --- Actor status response ---
        if DuoFernDecoder.is_status_response(frame):
            self._handle_status(frame)
            return

        _LOGGER.debug("Unhandled frame 0x%02X: %s", frame[0], frame_to_hex(frame))

    # ------------------------------------------------------------------
    # Status handling
    # ------------------------------------------------------------------

    def _handle_status(self, frame: bytearray) -> None:
        """Parse a status frame and update device state."""
        device_code = DuoFernDecoder.extract_device_code_from_status(frame)

        # Multi-channel devices: channel comes from frame byte 1
        # For channel devices the frame byte 1 = channel number (01, 02, ...)
        # For single-channel: byte 1 = 0xFF (broadcast) or 0x01
        channel_byte = frame[1]
        if channel_byte not in (0xFF, 0x00, 0x01):
            channel = f"{channel_byte:02X}"
        else:
            channel = "01"

        # Try channel-specific key first, then base key
        ch_id = device_code.with_channel(channel)
        state = self._data.devices.get(ch_id.full_hex)
        if state is None:
            state = self._data.devices.get(device_code.hex)
        if state is None:
            _LOGGER.debug("Status from unknown device %s, ignoring", device_code.hex)
            return

        parsed = DuoFernDecoder.parse_status(frame, channel=channel)
        parsed.channel = channel

        state.status = parsed
        state.available = True
        state.last_seen = time.time()

        _LOGGER.debug(
            "Status %s ch=%s pos=%s level=%s moving=%s",
            device_code.hex,
            channel,
            parsed.position,
            parsed.level,
            parsed.moving,
        )

        self.async_set_updated_data(self._data)

    # ------------------------------------------------------------------
    # Sensor event -> HA event bus
    # ------------------------------------------------------------------

    def _fire_sensor_event(self, ev: SensorEvent) -> None:
        """Fire a DuoFern sensor/button event on the HA event bus.

        Event type: duofern_event
        Event data: {
          "device_code": "A31234",
          "channel":     "01",
          "event":       "up",
          "state":       "Btn01",   # optional
        }
        """
        event_data: dict[str, Any] = {
            "device_code": ev.device_code,
            "channel": ev.channel,
            "event": ev.event_name,
        }
        if ev.state is not None:
            event_data["state"] = ev.state

        _LOGGER.debug("Firing %s: %s", DUOFERN_EVENT, event_data)
        self.hass.bus.async_fire(DUOFERN_EVENT, event_data)

    # ------------------------------------------------------------------
    # Reconnect (after NOT INITIALIZED)
    # ------------------------------------------------------------------

    async def _reconnect(self) -> None:
        """Disconnect and reconnect the stick (triggered by NOT INITIALIZED)."""
        _LOGGER.warning("Reconnecting DuoFern stick due to NOT INITIALIZED error")
        try:
            if self._stick:
                await self._stick.disconnect()
            await asyncio.sleep(2)
            await self._stick.connect()  # type: ignore[union-attr]
            _LOGGER.info("DuoFern stick reconnected successfully")
        except Exception:
            _LOGGER.exception("Failed to reconnect DuoFern stick")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_state(self, device_code: DuoFernId) -> DuoFernDeviceState | None:
        """Find device state by base code or full channel code."""
        state = self._data.devices.get(device_code.full_hex)
        if state is None:
            state = self._data.devices.get(device_code.hex)
        return state

    def _optimistic_moving(self, device_code: DuoFernId, direction: str) -> None:
        """Optimistically update moving state before status arrives."""
        state = self._find_state(device_code)
        if state:
            state.status.moving = direction
            self.async_set_updated_data(self._data)

    async def _send(self, frame: bytearray) -> None:
        """Send a frame via the stick."""
        if self._stick is None or not self._stick.connected:
            _LOGGER.error("Cannot send: stick not connected")
            return
        await self._stick.send_command(frame)

    # ------------------------------------------------------------------
    # Diagnostics helper (used by diagnostics.py)
    # ------------------------------------------------------------------

    def get_diagnostics(self) -> dict[str, Any]:
        """Return a snapshot of all device states for HA diagnostics."""
        devices: dict[str, Any] = {}
        for key, state in self._data.devices.items():
            devices[key] = {
                "device_code": state.device_code.hex,
                "channel": state.channel,
                "device_type": f"0x{state.device_code.device_type:02X}",
                "device_type_name": state.device_code.device_type_name,
                "available": state.available,
                "last_seen": state.last_seen,
                "battery_state": state.battery_state,
                "battery_percent": state.battery_percent,
                "readings": {k: v for k, v in state.status.readings.items()},
                "position": state.status.position,
                "level": state.status.level,
                "moving": state.status.moving,
                "version": state.status.version,
                "measured_temp": state.status.measured_temp,
                "desired_temp": state.status.desired_temp,
            }
        return {
            "system_code": self._system_code.hex,
            "port": self._port,
            "device_count": len(self._data.devices),
            "pairing_active": self._data.pairing_active,
            "unpairing_active": self._data.unpairing_active,
            "devices": devices,
        }
