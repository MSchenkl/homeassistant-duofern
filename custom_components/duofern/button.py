"""Button platform for DuoFern — Stick control buttons.

Adds three buttons to the DuoFern Stick device:
  - "Pairing starten"          (60s pairing window, auto-stop)
  - "Unpairing starten"        (60s unpairing window, auto-stop)
  - "Status aller Geräte"      (broadcast status request)

All three buttons appear on the Stick device card in the HA dashboard.
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DuoFernConfigEntry
from .const import DOMAIN
from .coordinator import DuoFernCoordinator, DuoFernData

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DuoFernConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up DuoFern stick button entities."""
    coordinator: DuoFernCoordinator = entry.runtime_data
    system_code_hex = coordinator.system_code.hex

    async_add_entities(
        [
            DuoFernPairButton(coordinator, system_code_hex),
            DuoFernUnpairButton(coordinator, system_code_hex),
            DuoFernStatusButton(coordinator, system_code_hex),
        ]
    )


# ---------------------------------------------------------------------------
# Shared device info for all stick buttons
# ---------------------------------------------------------------------------


def _stick_device_info(
    coordinator: DuoFernCoordinator, system_code_hex: str
) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, system_code_hex)},
    )


# ---------------------------------------------------------------------------
# Pairing button
# ---------------------------------------------------------------------------


class DuoFernPairButton(CoordinatorEntity[DuoFernCoordinator], ButtonEntity):
    """Button to start pairing mode on the DuoFern stick."""

    _attr_has_entity_name = True
    _attr_translation_key = "start_pairing"

    def __init__(self, coordinator: DuoFernCoordinator, system_code_hex: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{system_code_hex}_pair"
        self._attr_name = "Pairing starten"
        self._attr_icon = "mdi:link-plus"
        self._attr_device_info = _stick_device_info(coordinator, system_code_hex)

    @property
    def available(self) -> bool:
        """Only available when not already in pair/unpair mode."""
        if self.coordinator.data is None:
            return False
        d = self.coordinator.data
        return not d.pairing_active and not d.unpairing_active

    async def async_press(self) -> None:
        """Start 60s pairing window."""
        await self.coordinator.async_start_pairing()


# ---------------------------------------------------------------------------
# Unpairing button
# ---------------------------------------------------------------------------


class DuoFernUnpairButton(CoordinatorEntity[DuoFernCoordinator], ButtonEntity):
    """Button to start unpairing mode on the DuoFern stick."""

    _attr_has_entity_name = True
    _attr_translation_key = "start_unpairing"

    def __init__(self, coordinator: DuoFernCoordinator, system_code_hex: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{system_code_hex}_unpair"
        self._attr_name = "Unpairing starten"
        self._attr_icon = "mdi:link-off"
        self._attr_device_info = _stick_device_info(coordinator, system_code_hex)

    @property
    def available(self) -> bool:
        if self.coordinator.data is None:
            return False
        d = self.coordinator.data
        return not d.pairing_active and not d.unpairing_active

    async def async_press(self) -> None:
        """Start 60s unpairing window."""
        await self.coordinator.async_start_unpairing()


# ---------------------------------------------------------------------------
# Status broadcast button
# ---------------------------------------------------------------------------


class DuoFernStatusButton(CoordinatorEntity[DuoFernCoordinator], ButtonEntity):
    """Button to request fresh status from all paired devices."""

    _attr_has_entity_name = True
    _attr_translation_key = "request_status"

    def __init__(self, coordinator: DuoFernCoordinator, system_code_hex: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{system_code_hex}_status"
        self._attr_name = "Status aller Geräte"
        self._attr_icon = "mdi:refresh"
        self._attr_device_info = _stick_device_info(coordinator, system_code_hex)

    async def async_press(self) -> None:
        """Send broadcast status request to all paired devices."""
        await self.coordinator.async_request_status()
