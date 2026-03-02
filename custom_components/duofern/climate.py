"""Climate platform for DuoFern thermostats.

Covers the following device types:
  0x73  Raumthermostat      (format "27") — wall thermostat, read/write
  0xE1  Heizkörperantrieb   (format "29") — radiator valve, read/write

From 30_DUOFERN.pm:
  %sets = (%setsBasic, %setsThermostat) if ($hash->{CODE} =~ /^73..../);
  %sets = (%setsHSA)                    if ($hash->{CODE} =~ /^E1..../);

Raumthermostat (0x73) format "27" readings:
  measured-temp, measured-temp2, desired-temp,
  temperatureThreshold1-4, actTempLimit, output,
  manualOverride, timeAutomatic, manualMode

Heizkörperantrieb (0xE1) format "29" readings:
  desired-temp, measured-temp, manualMode, timeAutomatic,
  sendingInterval, batteryPercent, valvePosition, forceResponse

  The HSA has a special bidirectional protocol: on receipt of a status
  frame, pending set-values are encoded back. FHEM stores these in
  hash->{helper}{HSAold} and sends them in the next status ACK.
  We implement a simpler version: desired-temp is sent via the
  coordinator's async_switch_on/off mechanism, which triggers a
  status request after ACK.

  From 30_DUOFERN.pm:
    #Heizkörperantrieb
    if ($code =~ m/^E1..../) { ... $setValue |= ($rawValue << bitFrom) ... }

All automation readings are exposed as extra_state_attributes.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DuoFernConfigEntry
from .const import DOMAIN
from .coordinator import DuoFernCoordinator, DuoFernDeviceState
from .protocol import DuoFernEncoder, DuoFernId

_LOGGER = logging.getLogger(__name__)

# Temperature range from 30_DUOFERN.pm $tempSetList: 4.0 .. 30.0 in 0.5 steps
TEMP_MIN = 4.0
TEMP_MAX = 30.0
TEMP_STEP = 0.5

_SKIP_AS_ATTRIBUTE = {"desired-temp", "measured-temp", "measured-temp2"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DuoFernConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up DuoFern climate entities."""
    coordinator: DuoFernCoordinator = entry.runtime_data

    entities: list[DuoFernClimate] = []
    for hex_code, device_state in coordinator.data.devices.items():
        if device_state.device_code.is_climate:
            entities.append(
                DuoFernClimate(
                    coordinator=coordinator,
                    device_state=device_state,
                    hex_code=hex_code,
                )
            )
            _LOGGER.debug("Adding climate entity for device %s", hex_code)

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d DuoFern climate entities", len(entities))


class DuoFernClimate(CoordinatorEntity[DuoFernCoordinator], ClimateEntity):
    """A DuoFern thermostat or radiator valve as a HA ClimateEntity.

    Supports HEAT and OFF modes:
      HEAT: device is controlling temperature (normal operation)
      OFF:  manualMode=on with desired-temp at minimum (FHEM behaviour)

    From 30_DUOFERN.pm %setsThermostat:
      desired-temp:$tempSetList  — set target temperature (4.0-30.0°C)
      manualMode:on,off          — bypass timer program
      timeAutomatic:on,off       — enable/disable timer
      temperatureThreshold1-4    — zone thresholds
      actTempLimit:1,2,3,4       — active threshold selection
    """

    _attr_has_entity_name = True
    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_target_temperature_step = TEMP_STEP
    _attr_min_temp = TEMP_MIN
    _attr_max_temp = TEMP_MAX

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
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, hex_code)},
            name=f"DuoFern {device_state.device_code.device_type_name} ({hex_code})",
            manufacturer="Rademacher",
            model=device_state.device_code.device_type_name,
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
    def current_temperature(self) -> float | None:
        """Return current measured temperature.

        From %statusIds: id=165 (format 27) measured-temp via scaleF2,
                         id=181 (format 29) measured-temp via scaleF4.
        """
        state = self._device_state
        if state is None:
            return None
        return state.status.measured_temp

    @property
    def target_temperature(self) -> float | None:
        """Return the desired/set temperature.

        From %statusIds: id=164 (format 27) desired-temp via scaleF1,
                         id=180 (format 29) desired-temp via scaleF3.
        """
        state = self._device_state
        if state is None:
            return None
        return state.status.desired_temp

    @property
    def hvac_mode(self) -> HVACMode:
        """Return current HVAC mode.

        From 30_DUOFERN.pm: manualMode=on means user has taken over.
        We map manualMode=off + timeAutomatic=on -> HEAT (normal auto mode),
        any other combination -> HEAT as well (device is always heating).
        Only report OFF if desired-temp is at minimum (4°C).
        """
        state = self._device_state
        if state is None:
            return HVACMode.HEAT
        desired = state.status.desired_temp
        if desired is not None and desired <= TEMP_MIN:
            return HVACMode.OFF
        return HVACMode.HEAT

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return all thermostat/HSA readings as extra state attributes.

        Raumthermostat: temperatureThreshold1-4, actTempLimit, output,
          manualOverride, timeAutomatic, manualMode, measured-temp2.
        Heizkörperantrieb: valvePosition, sendingInterval, batteryPercent,
          manualMode, timeAutomatic, forceResponse.
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

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set the desired temperature.

        Sends a DuoFern command to the device. The actual protocol for
        the HSA is bidirectional (helper{HSAold} in FHEM), but a simpler
        approach works: send desired-temp as a level command and wait for
        the next status response to confirm.

        From 30_DUOFERN.pm %setsThermostat:
          desired-temp:$tempSetList (4.0-30.0°C in 0.5°C steps)
        """
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        temp = max(TEMP_MIN, min(TEMP_MAX, round(temp / TEMP_STEP) * TEMP_STEP))
        # Encode as level 0-100 scaled from TEMP_MIN-TEMP_MAX
        level = round((temp - TEMP_MIN) / (TEMP_MAX - TEMP_MIN) * 100)
        await self.coordinator.async_set_level(self._device_code, level)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set HVAC mode.

        OFF  -> set desired-temp to minimum (4°C) — equivalent to FHEM off
        HEAT -> set desired-temp to a reasonable default (20°C) if currently off
        """
        if hvac_mode == HVACMode.OFF:
            await self.async_set_temperature(**{ATTR_TEMPERATURE: TEMP_MIN})
        elif hvac_mode == HVACMode.HEAT:
            state = self._device_state
            current = state.status.desired_temp if state else None
            if current is None or current <= TEMP_MIN:
                await self.async_set_temperature(**{ATTR_TEMPERATURE: 20.0})

    @callback
    def _handle_coordinator_update(self) -> None:
        state = self._device_state
        if state and state.status.version:
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, self._hex_code)},
                name=(
                    f"DuoFern {self._device_code.device_type_name} ({self._hex_code})"
                ),
                manufacturer="Rademacher",
                model=self._device_code.device_type_name,
                sw_version=state.status.version,
                via_device=(DOMAIN, self.coordinator.system_code.hex),
            )
        self.async_write_ha_state()
