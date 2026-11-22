"""Apartment Comfort integration."""

from .const import PLATFORMS
from .coordinator import ComfortCoordinator
from .export import setup_downloads


async def async_setup_entry(hass, entry):
    setup_downloads(hass)
    coordinator = ComfortCoordinator(hass, entry)
    entry.runtime_data = coordinator
    try:
        await coordinator.async_initialize()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await coordinator.debug.async_close()
        raise
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
