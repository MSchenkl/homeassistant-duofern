"""Switch platform for DuoFern switch actors and the Universalaktor.

Covers the following device types:
  0x43  Universalaktor     (2 channels: 01 and 02 — each gets own SwitchEntity)
  0x46  Steckdosenaktor    (single channel)
  0x71  Troll Comfort DuoFern (Lichtmodus)

The Universalaktor (0x43) is the "universal actor" — it can switch any load
including lights, sockets, or motors. In FHEM it is represented as two separate
sub-devices (6-digit code + "01" / "02"). We do the same here: two SwitchEntities
per Universalaktor, both grouped under the same parent device in HA.

From 30_DUOFERN.pm:
  %sets = (%setsSwitchActor, %setsPair)  if ($hash->{CODE} =~ /^43....(01|02)/);
  %sets = (%setsBasic, %setsSwitchActor) if ($hash->{CODE} =~ /^(46|71)..../);

All readings (dawnAutomatic, duskAutomatic, sunAutomatic, timeAutomatic,
manualMode, sunMode, stairwellFunction, stairwellTime, modeChange) are
exposed as extra_state_attributes.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DuoFernConfigEntry
from .const import DOMAIN
from .coordinator import DuoFernCoordinator, DuoFernDeviceState
from .protocol import DuoFernId

_LOGGER = logging.getLogger(__name__)

# Readings exposed as extra_state_attributes — all except the primary on/off level
_SKIP_AS_ATTRIBUTE = {"level"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DuoFernConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up DuoFern switch entities.

    For the Universalaktor (0x43) each channel (01, 02) becomes its own entity.
    For Steckdosenaktor (0x46) and Troll Lichtmodus (0x71) one entity per device.
    """
    coordinator: DuoFernCoordinator = entry.runtime_data

    entities: list[DuoFernSwitch] = []
    for hex_code, device_state in coordinator.data.devices.items():
        if device_state.device_code.is_switch:
            entities.append(
                DuoFernSwitch(
                    coordinator=coordinator,
                    device_state=device_state,
                    hex_code=hex_code,
                    entry_id=entry.entry_id,
                )
            )
            _LOGGER.debug("Adding switch entity for device %s", hex_code)

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d DuoFern switch entities", len(entities))


class DuoFernSwitch(CoordinatorEntity[DuoFernCoordinator], SwitchEntity):
    """A DuoFern switch actor channel as a HA SwitchEntity.

    For the Universalaktor, hex_code is the 8-char channel code (e.g. 43ABCD01).
    For single-channel devices, hex_code is the 6-char device code.

    From 30_DUOFERN.pm %setsSwitchActor:
      on, off, dawnAutomatic, duskAutomatic, manualMode, sunAutomatic,
      timeAutomatic, sunMode, modeChange, stairwellFunction, stairwellTime,
      dusk, dawn
    """

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(
        self,
        coordinator: DuoFernCoordinator,
        device_state: DuoFernDeviceState,
        hex_code: str,
        entry_id: str,
    ) -> None:
        super().__init__(coordinator)

        self._hex_code = hex_code
        self._device_code = device_state.device_code
        self._channel = device_state.channel

        self._attr_unique_id = f"{DOMAIN}_{hex_code}"

        # Channel number as int for encoder (01 -> 1, 02 -> 2)
        self._channel_int = int(self._channel, 16) if self._channel else 1

        # Device class: OUTLET for socket actor, SWITCH for all others
        # From 30_DUOFERN.pm: 0x46 = Steckdosenaktor (socket)
        if self._device_code.device_type == 0x46:
            self._attr_device_class = SwitchDeviceClass.OUTLET
        else:
            self._attr_device_class = SwitchDeviceClass.SWITCH

        # Channel label for multi-channel devices
        if self._channel and self._device_code.has_channels:
            channel_label = f" Kanal {self._channel}"
        else:
            channel_label = ""

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, hex_code)},
            name=(
                f"DuoFern {self._device_code.device_type_name}"
                f" ({self._device_code.hex}){channel_label}"
            ),
            manufacturer="Rademacher",
            model=self._device_code.device_type_name,
            via_device=(DOMAIN, coordinator.system_code.hex),
        )

    @property
    def _device_state(self) -> DuoFernDeviceState | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.devices.get(self._hex_code)

    @property
    def available(self) -> bool:
        state = self._device_state
        if state is None:
            return False
        return state.available and self.coordinator.last_update_success

    @property
    def is_on(self) -> bool | None:
        """Return True if the switch is on (level > 0).

        From 30_DUOFERN.pm %statusIds id=1:
          "level" -> 0-100 where 0=off, >0=on
        """
        state = self._device_state
        if state is None:
            return None
        level = state.status.level
        if level is None:
            return None
        return level > 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return all automation readings as extra state attributes.

        Exposes: dawnAutomatic, duskAutomatic, sunAutomatic, timeAutomatic,
        manualMode, sunMode, modeChange, stairwellFunction, stairwellTime.
        All are present in %setsSwitchActor / %statusIds in 30_DUOFERN.pm.
        """
        state = self._device_state
        if state is None:
            return {}
        attrs: dict[str, Any] = {
            k: v
            for k, v in state.status.readings.items()
            if k not in _SKIP_AS_ATTRIBUTE
        }
        if state.status.version:
            attrs["firmware_version"] = state.status.version
        if state.battery_state is not None:
            attrs["battery_state"] = state.battery_state
        if state.battery_percent is not None:
            attrs["battery_percent"] = state.battery_percent
        return attrs

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on.

        From 30_DUOFERN.pm %commands: on => cmd => {val => "0E03"}
        """
        await self.coordinator.async_switch_on(
            self._device_code, channel=self._channel_int
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off.

        From 30_DUOFERN.pm %commands: off => cmd => {val => "0E02"}
        """
        await self.coordinator.async_switch_off(
            self._device_code, channel=self._channel_int
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        state = self._device_state
        if state and state.status.version:
            channel_label = (
                f" Kanal {self._channel}"
                if self._channel and self._device_code.has_channels
                else ""
            )
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, self._hex_code)},
                name=(
                    f"DuoFern {self._device_code.device_type_name}"
                    f" ({self._device_code.hex}){channel_label}"
                ),
                manufacturer="Rademacher",
                model=self._device_code.device_type_name,
                sw_version=state.status.version,
                via_device=(DOMAIN, self.coordinator.system_code.hex),
            )
        self.async_write_ha_state()
