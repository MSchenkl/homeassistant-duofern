"""Binary sensor platform for DuoFern motion, smoke, and contact sensors.

Covers the following device types:
  0x65  Bewegungsmelder     (motion detector)
  0xAB  Rauchmelder         (smoke detector)
  0xAC  Fenster-Tuer-Kontakt (window/door contact)

These devices do not send periodic status — they fire sensor events
(SENSOR_MESSAGES in const.py) when triggered. The coordinator receives
these via _fire_sensor_event() and fires HA events on the event bus.

This platform creates a BinarySensorEntity per device that:
  1. Shows the current state (on/off) based on the last received event
  2. Listens to duofern_event on the HA event bus to update state
  3. Exposes battery_state and battery_percent as extra_state_attributes
     when available (from %sensorMsg battery status frame)

From 30_DUOFERN.pm %sensorMsg:
  0720 startMotion -> on    (Bewegungsmelder)
  0721 endMotion   -> off
  071E startSmoke  -> on    (Rauchmelder)
  071F endSmoke    -> off
  0723 opened      -> opened (Fensterkontakt)
  0724 closed      -> closed
  0725 startVibration
  0726 endVibration
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DuoFernConfigEntry
from .const import DOMAIN
from .coordinator import DUOFERN_EVENT, DuoFernCoordinator, DuoFernDeviceState

_LOGGER = logging.getLogger(__name__)

# Map event names to binary on/off state.
# From %sensorMsg in 30_DUOFERN.pm.
_EVENT_TO_STATE: dict[str, bool] = {
    "startMotion": True,
    "endMotion": False,
    "startSmoke": True,
    "endSmoke": False,
    "startRain": True,
    "endRain": False,
    "startVibration": True,
    "endVibration": False,
    "opened": True,  # Fensterkontakt: open = True
    "closed": False,
}

# Device class per device type byte
_DEVICE_CLASS: dict[int, BinarySensorDeviceClass] = {
    0x65: BinarySensorDeviceClass.MOTION,  # Bewegungsmelder
    0xAB: BinarySensorDeviceClass.SMOKE,  # Rauchmelder
    0xAC: BinarySensorDeviceClass.OPENING,  # Fenster-Tuer-Kontakt
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DuoFernConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up DuoFern binary sensor entities."""
    coordinator: DuoFernCoordinator = entry.runtime_data

    entities: list[DuoFernBinarySensor] = []
    for hex_code, device_state in coordinator.data.devices.items():
        if device_state.device_code.is_binary_sensor:
            entities.append(
                DuoFernBinarySensor(
                    coordinator=coordinator,
                    device_state=device_state,
                    hex_code=hex_code,
                )
            )
            _LOGGER.debug("Adding binary sensor entity for device %s", hex_code)

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d DuoFern binary sensor entities", len(entities))


class DuoFernBinarySensor(CoordinatorEntity[DuoFernCoordinator], BinarySensorEntity):
    """A DuoFern motion/smoke/contact sensor as a HA BinarySensorEntity.

    State is updated via HA event bus (duofern_event) rather than coordinator
    data, because these devices only send events — not periodic status frames.

    From 30_DUOFERN.pm:
      #Wandtaster, Funksender UP, Handsender, Sensoren
      Events are dispatched via DUOFERN_Parse -> Dispatch -> here.
    """

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(
        self,
        coordinator: DuoFernCoordinator,
        device_state: DuoFernDeviceState,
        hex_code: str,
    ) -> None:
        super().__init__(coordinator)
        self._hex_code = hex_code
        self._device_code = device_state.device_code
        self._attr_unique_id = f"{DOMAIN}_{hex_code}"
        self._is_on: bool | None = None

        self._attr_device_class = _DEVICE_CLASS.get(
            self._device_code.device_type,
            BinarySensorDeviceClass.MOTION,
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, hex_code)},
            name=f"DuoFern {device_state.device_code.device_type_name} ({hex_code})",
            manufacturer="Rademacher",
            model=device_state.device_code.device_type_name,
            via_device=(DOMAIN, coordinator.system_code.hex),
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to DuoFern events on the HA event bus."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.hass.bus.async_listen(DUOFERN_EVENT, self._handle_duofern_event)
        )

    @property
    def _device_state(self) -> DuoFernDeviceState | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.devices.get(self._hex_code)

    @property
    def available(self) -> bool:
        state = self._device_state
        return state is not None

    @property
    def is_on(self) -> bool | None:
        """Return current binary state (True=triggered, False=clear)."""
        return self._is_on

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return battery info as extra state attributes.

        From 30_DUOFERN.pm:
          #Sensoren Batterie (0FFF1323...)
          batteryState: ok | low
          batteryPercent: 0-100
        """
        state = self._device_state
        if state is None:
            return {}
        attrs: dict[str, Any] = {}
        if state.battery_state is not None:
            attrs["battery_state"] = state.battery_state
        if state.battery_percent is not None:
            attrs["battery_percent"] = state.battery_percent
        if state.last_seen is not None:
            attrs["last_seen"] = state.last_seen
        return attrs

    @callback
    def _handle_duofern_event(self, event: Event) -> None:
        """Handle a duofern_event from the HA event bus.

        Only processes events for this device's code.
        Maps event names to binary on/off using _EVENT_TO_STATE.

        From 30_DUOFERN.pm %sensorMsg event dispatch.
        """
        data = event.data
        if data.get("device_code") != self._hex_code:
            return

        event_name: str = data.get("event", "")
        new_state = _EVENT_TO_STATE.get(event_name)
        if new_state is not None:
            self._is_on = new_state
            self.async_write_ha_state()
            _LOGGER.debug(
                "Binary sensor %s: %s -> %s",
                self._hex_code,
                event_name,
                new_state,
            )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update battery state from coordinator data."""
        self.async_write_ha_state()
