"""Cover platform for DuoFern roller shutters.

Supports all DuoFern cover device types and status formats:
  Format 21:  RolloTron Standard / Comfort (0x40, 0x41, 0x61)
  Format 23:  Rohrmotor-Aktor, Connect-Aktor, Troll Basis,
              Troll Comfort (0x42, 0x49, 0x4B, 0x4C, 0x70)
  Format 23a: Rohrmotor Steuerung (0x47) — format override in const.py
  Format 24a: SX5 garage door (0x4E) — format override in const.py

Each device becomes one CoverEntity with:
  - Open / Close / Stop / Set Position
  - Position reporting (0 = closed, 100 = open in HA convention)
  - Moving state (opening / closing / stopped)
  - Extra state attributes: all readings from the status frame
    (sunMode, ventilatingPosition, manualMode, timeAutomatic, etc.)
  - Device info linked to the hub (USB stick) via via_device

Position convention (matches existing HA addon behaviour):
  DuoFern native: 0 = fully open, 100 = fully closed
  Home Assistant: 0 = fully closed, 100 = fully open

  From 30_DUOFERN.pm (without positionInverse attr, default behaviour):
    $state = "opened" if ($state eq "0");
    $state = "closed" if ($state eq "100");

  This module always inverts — same as the original HA addon cover.py:
    current_cover_position: return 100 - state.status.position
    async_set_cover_position: duofern_position = 100 - ha_position

Device class per format:
  Format 21/23/23a: CoverDeviceClass.SHUTTER (roller shutter)
  Format 24a:       CoverDeviceClass.GARAGE   (SX5 garage door)
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.cover import (
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DuoFernConfigEntry
from .const import (
    COVER_DEVICE_TYPES_FORMAT24,
    DOMAIN,
)
from .coordinator import DuoFernCoordinator, DuoFernDeviceState
from .protocol import DuoFernId

_LOGGER = logging.getLogger(__name__)

# Readings that are exposed as extra state attributes.
# All other readings from ParsedStatus.readings are included automatically.
# These are excluded because they are already first-class HA properties:
_SKIP_AS_ATTRIBUTE = {"position", "moving"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DuoFernConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up DuoFern cover entities from a config entry.

    One CoverEntity per paired cover device. Channel devices are not
    covers, so no channel-splitting needed here.
    """
    coordinator: DuoFernCoordinator = entry.runtime_data

    entities: list[DuoFernCover] = []
    for hex_code, device_state in coordinator.data.devices.items():
        if device_state.device_code.is_cover:
            entities.append(
                DuoFernCover(
                    coordinator=coordinator,
                    device_code=device_state.device_code,
                    entry_id=entry.entry_id,
                )
            )
            _LOGGER.debug("Adding cover entity for device %s", hex_code)

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d DuoFern cover entities", len(entities))
    else:
        _LOGGER.warning("No cover devices found in paired device list")


class DuoFernCover(CoordinatorEntity[DuoFernCoordinator], CoverEntity):
    """Representation of a DuoFern roller shutter or garage door as a CoverEntity.

    Inherits from CoordinatorEntity for automatic state updates when the
    coordinator calls async_set_updated_data() on incoming status frames.

    From 30_DUOFERN.pm set commands per device type:
      RolloTron (0x40/0x41/0x61): %setsBasic + %setsDefaultRollerShutter
      Rohrmotor/Troll (0x42/0x4B/0x4C/0x70): + %setsTroll + blindsMode
      Rohrmotor Steuerung (0x47): + %setsTroll (no blindsMode)
      Rohrmotor (0x49): + %setsRolloTube
      SX5 (0x4E): %setsSX5
    All of these are captured in extra_state_attributes from status readings.
    """

    _attr_has_entity_name = True
    _attr_name = None  # Use device name as entity name

    def __init__(
        self,
        coordinator: DuoFernCoordinator,
        device_code: DuoFernId,
        entry_id: str,
    ) -> None:
        """Initialize the cover entity."""
        super().__init__(coordinator)

        self._device_code = device_code
        self._hex_code = device_code.hex

        # Unique ID: domain + device code
        self._attr_unique_id = f"{DOMAIN}_{self._hex_code}"

        # Device class: GARAGE for SX5, SHUTTER for all others
        # From 30_DUOFERN.pm: SX5 (0x4E) uses format "24a" (garage door)
        if device_code.device_type in COVER_DEVICE_TYPES_FORMAT24:
            self._attr_device_class = CoverDeviceClass.GARAGE
        else:
            self._attr_device_class = CoverDeviceClass.SHUTTER

        # All cover types support open/close/stop/set_position.
        # From 30_DUOFERN.pm %setsBasic + %setsDefaultRollerShutter + %setsSX5:
        #   up, down, stop, position (slider 0-100)
        self._attr_supported_features = (
            CoverEntityFeature.OPEN
            | CoverEntityFeature.CLOSE
            | CoverEntityFeature.STOP
            | CoverEntityFeature.SET_POSITION
        )

        # Device info — firmware version updated on first status frame
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._hex_code)},
            name=f"DuoFern {device_code.device_type_name} ({self._hex_code})",
            manufacturer="Rademacher",
            model=device_code.device_type_name,
            sw_version=None,
            via_device=(DOMAIN, coordinator.system_code.hex),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @property
    def _device_state(self) -> DuoFernDeviceState | None:
        """Return current device state from coordinator data."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.devices.get(self._hex_code)

    # ------------------------------------------------------------------
    # CoverEntity properties
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """Return True if the device is available.

        False if the coordinator lost contact (MISSING ACK 810108AA)
        or the coordinator itself failed.
        """
        state = self._device_state
        if state is None:
            return False
        return state.available and self.coordinator.last_update_success

    @property
    def current_cover_position(self) -> int | None:
        """Return current position (HA convention: 0=closed, 100=open).

        DuoFern native (from %statusIds invert=100): 0=open, 100=closed.
        HA convention (matches existing addon): 0=closed, 100=open.
        Inversion: ha_position = 100 - duofern_position

        From 30_DUOFERN.pm (default, positionInverse not set):
          $state = "opened" if ($state eq "0");   # DuoFern 0 = open
          $state = "closed" if ($state eq "100"); # DuoFern 100 = closed
        """
        state = self._device_state
        if state is None or state.status.position is None:
            return None
        return 100 - state.status.position

    @property
    def is_closed(self) -> bool | None:
        """Return True if the cover is fully closed (HA position == 0)."""
        pos = self.current_cover_position
        if pos is None:
            return None
        return pos == 0

    @property
    def is_opening(self) -> bool:
        """Return True if the cover is currently opening (moving up).

        From 30_DUOFERN.pm:
          readingsSingleUpdate($hash, "moving", "up", 1) if ($cmd eq "up")
        """
        state = self._device_state
        if state is None:
            return False
        return state.status.moving == "up"

    @property
    def is_closing(self) -> bool:
        """Return True if the cover is currently closing (moving down).

        From 30_DUOFERN.pm:
          readingsSingleUpdate($hash, "moving", "down", 1) if ($cmd eq "down")
        """
        state = self._device_state
        if state is None:
            return False
        return state.status.moving == "down"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return all device readings as extra state attributes.

        Exposes everything from ParsedStatus.readings that is not already
        a first-class HA property (position, moving).

        This includes all readings from the status format:
          Format 21: sunMode, ventilatingMode, ventilatingPosition,
                     sunPosition, timeAutomatic, duskAutomatic,
                     dawnAutomatic, manualMode, runningTime
          Format 23/23a: + windAutomatic, rainAutomatic, windMode,
                     rainMode, windDirection, rainDirection, reversal,
                     motorDeadTime, runningTime, sunMode, sunPosition,
                     ventilatingMode, ventilatingPosition
          Format 23 blinds: + slatPosition, slatRunTime, blindsMode,
                     tiltInSunPos, tiltInVentPos, tiltAfterMoveLevel,
                     tiltAfterStopDown, defaultSlatPos
          Format 24a (SX5): obstacle, block, lightCurtain,
                     automaticClosing, openSpeed, 2000cycleAlarm,
                     wicketDoor, backJump, 10minuteAlarm, light

        Battery info is also included if available.
        """
        state = self._device_state
        if state is None:
            return {}

        attrs: dict[str, Any] = {}

        # All readings except those already exposed as HA properties
        for key, value in state.status.readings.items():
            if key not in _SKIP_AS_ATTRIBUTE:
                attrs[key] = value

        # Firmware version
        if state.status.version:
            attrs["firmware_version"] = state.status.version

        # Battery info (from %sensorMsg battery status frame)
        if state.battery_state is not None:
            attrs["battery_state"] = state.battery_state
        if state.battery_percent is not None:
            attrs["battery_percent"] = state.battery_percent

        # Last seen timestamp
        if state.last_seen is not None:
            attrs["last_seen"] = state.last_seen

        return attrs

    # ------------------------------------------------------------------
    # CoverEntity commands
    # ------------------------------------------------------------------

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover (move up).

        From 30_DUOFERN.pm %commands:
          up => cmd => { "" => {val => "0701", ...} }
        """
        await self.coordinator.async_cover_up(self._device_code)

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover (move down).

        From 30_DUOFERN.pm %commands:
          down => cmd => { "" => {val => "0703", ...} }
        """
        await self.coordinator.async_cover_down(self._device_code)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover movement.

        From 30_DUOFERN.pm %commands:
          stop => cmd => { "" => {val => "0702", ...} }
        """
        await self.coordinator.async_cover_stop(self._device_code)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the cover to a specific position.

        HA sends: 0 = closed, 100 = open.
        DuoFern expects: 0 = open, 100 = closed.
        Inversion: duofern_position = 100 - ha_position

        From 30_DUOFERN.pm %commands:
          position => cmd => { "" => {val => "0707", ...} }
        And the set handler:
          if ($positionInverse eq "1") { $position = 100 - $position; }
        We always invert (HA convention), matching the existing addon.
        """
        ha_position: int = kwargs.get("position", 0)
        duofern_position = 100 - ha_position
        await self.coordinator.async_cover_position(self._device_code, duofern_position)

    # ------------------------------------------------------------------
    # Coordinator entity callbacks
    # ------------------------------------------------------------------

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator.

        Called automatically by CoordinatorEntity when the coordinator
        calls async_set_updated_data() after a status frame arrives.
        Updates firmware version in device registry when first received.
        """
        state = self._device_state
        if state and state.status.version:
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, self._hex_code)},
                name=f"DuoFern {self._device_code.device_type_name} ({self._hex_code})",
                manufacturer="Rademacher",
                model=self._device_code.device_type_name,
                sw_version=state.status.version,
                via_device=(DOMAIN, self.coordinator.system_code.hex),
            )

        self.async_write_ha_state()
