"""Apartment Comfort integration."""

from .const import PLATFORMS
from .coordinator import ComfortCoordinator


async def async_setup_entry(hass, entry):
    coordinator = ComfortCoordinator(hass, entry)
    entry.runtime_data = coordinator
    await coordinator.async_initialize()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.start()
    entry.async_on_unload(entry.add_update_listener(_reload))
    return True


async def _reload(hass, entry):
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass, entry):
    # Stop owned equipment before removing the state which tracks ownership.
    if not await entry.runtime_data.async_stop():
        return False
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
