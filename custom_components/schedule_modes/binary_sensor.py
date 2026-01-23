from __future__ import annotations
from datetime import timedelta
from typing import Optional, List
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.util import dt as dt_util
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import device_registry as dr

from .const import (
    DOMAIN, OPT_ENABLED_MODES, EVENT_MODE_KEYS, OPT_CUSTOM_CALENDARS,
    device_info_for_mode, device_info_main, mode_friendly, normalize_custom_calendars
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    enabled = entry.options.get(OPT_ENABLED_MODES, [])
    ents: List[BinarySensorEntity] = []

    # Create binary sensors for enabled modes
    for k in enabled:
        ents.append(ModeMirrorBinarySensor(hass, entry, k, mode_friendly(k), is_custom=False))
        ents.append(ModeEventActiveBinarySensor(hass, entry, k, mode_friendly(k), is_custom=False))

    # Create binary sensors for custom calendars (normalized format)
    custom_calendars = normalize_custom_calendars(entry.options.get(OPT_CUSTOM_CALENDARS, []))
    custom_cal_ids = []
    for custom_cal in custom_calendars:
        cal_id = custom_cal["id"]
        cal_name = custom_cal["name"]
        ents.append(ModeMirrorBinarySensor(hass, entry, cal_id, cal_name, is_custom=True))
        ents.append(ModeEventActiveBinarySensor(hass, entry, cal_id, cal_name, is_custom=True))
        custom_cal_ids.append(cal_id)

    # Clean up removed custom calendar binary sensor entities from entity registry
    registry = er.async_get(hass)
    all_entity_entries = er.async_entries_for_config_entry(registry, entry.entry_id)

    for entity_entry in all_entity_entries:
        # Check if this is a binary_sensor entity
        if entity_entry.domain == "binary_sensor":
            # Extract mode_key from unique_id
            # Format can be: {entry_id}_{mode_key}_mirror or {entry_id}_{mode_key}_event_active
            unique_id = entity_entry.unique_id

            # Skip non-mode binary sensors
            if unique_id.endswith("_event_modes") or unique_id.endswith("_event_running_with_override") or unique_id.endswith("_dst"):
                continue

            # Remove the entry_id prefix
            if unique_id.startswith(f"{entry.entry_id}_"):
                remainder = unique_id[len(f"{entry.entry_id}_"):]

                # Extract mode_key
                mode_key = None
                if remainder.endswith("_mirror"):
                    mode_key = remainder[:-len("_mirror")]
                elif remainder.endswith("_event_active"):
                    mode_key = remainder[:-len("_event_active")]

                if mode_key:
                    # Check if this mode/calendar is still in config
                    is_enabled_mode = mode_key in enabled
                    is_custom_calendar = mode_key in custom_cal_ids

                    # If not in either list, remove it
                    if not is_enabled_mode and not is_custom_calendar:
                        from logging import getLogger
                        _LOGGER = getLogger(__name__)
                        _LOGGER.debug("Removing orphaned binary_sensor entity: %s (mode_key: %s)",
                                     entity_entry.entity_id, mode_key)
                        registry.async_remove(entity_entry.entity_id)

    # Clean up orphaned devices (devices with no entities left)
    device_registry = dr.async_get(hass)
    all_devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)

    for device_entry in all_devices:
        # Check if this device has any entities left
        device_entities = er.async_entries_for_device(registry, device_entry.id, include_disabled_entities=True)

        # If the device has no entities, remove it
        if not device_entities:
            # Extract mode_key from device identifiers to log it
            mode_key = None
            for identifier_tuple in device_entry.identifiers:
                if len(identifier_tuple) == 3 and identifier_tuple[0] == DOMAIN:
                    mode_key = identifier_tuple[2]
                    break

            # Don't remove the main device
            if mode_key != "main":
                from logging import getLogger
                _LOGGER = getLogger(__name__)
                _LOGGER.debug("Removing orphaned device: %s (mode_key: %s)",
                             device_entry.name, mode_key)
                device_registry.async_remove_device(device_entry.id)

    # Combine enabled modes and custom calendars for summary sensors
    all_modes = list(enabled) + custom_cal_ids
    ents.append(EventModesSummaryBinarySensor(hass, entry, [k for k in enabled if k in EVENT_MODE_KEYS]))
    ents.append(EventRunningWithOverrideBinarySensor(hass, entry, all_modes))
    ents.append(DSTBinarySensor(hass, entry))
    async_add_entities(ents, True)


class ModeMirrorBinarySensor(BinarySensorEntity):
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, key: str, friendly: str, is_custom: bool = False):
        self.hass = hass
        self._entry = entry
        self._key = key
        self._friendly_name = friendly
        self._is_custom = is_custom
        self._attr_name = f"{friendly} Active"
        self._attr_unique_id = f"{entry.entry_id}_{key}_mirror"
        self._switch_eid = None  # Will be resolved dynamically from registry
        self._override_eid = None  # Will be resolved dynamically from registry
        self._active_helper = f"sensor.{DOMAIN}_active_{key}_event"
        self._next_helper = f"sensor.{DOMAIN}_next_{key}_event"
        self._controlled_by = "manual"
        self._next_start: Optional[str] = None
        self._next_end: Optional[str] = None
        self._active_start: Optional[str] = None
        self._active_end: Optional[str] = None
        self._last_ended: Optional[str] = None
        self._unsubs = []

    @property
    def device_info(self):
        # For custom calendars, use the custom name directly
        if self._is_custom:
            return {
                "identifiers": {(DOMAIN, self._entry.entry_id, self._key)},
                "manufacturer": "OnOff Automations",
                "name": self._friendly_name,
                "model": "Mode",
            }
        return device_info_for_mode(self._entry.entry_id, self._key)

    async def async_added_to_hass(self):
        # Resolve actual entity_ids from registry based on unique_ids
        registry = er.async_get(self.hass)

        # Find the mode switch entity by its unique_id
        switch_unique_id = f"{self._entry.entry_id}_{self._key}"
        for entity_id, entry in registry.entities.items():
            if entry.unique_id == switch_unique_id:
                self._switch_eid = entity_id
                break

        # Find the calendar override switch entity by its unique_id
        override_unique_id = f"{self._entry.entry_id}_{self._key}_calendar_override"
        for entity_id, entry in registry.entities.items():
            if entry.unique_id == override_unique_id:
                self._override_eid = entity_id
                break

        # Fallback to expected entity_ids if not found in registry (new entities)
        if not self._switch_eid:
            self._switch_eid = f"switch.{self._key}"
        if not self._override_eid:
            self._override_eid = f"switch.{self._key}_calendar_override"

        @callback
        def _on_change(_event):
            self._refresh_attrs()
            self.async_write_ha_state()

        # Track the mode switch, active/next helpers, and the calendar override switch for this mode
        self._unsubs.append(async_track_state_change_event(
            self.hass,
            [self._switch_eid, self._active_helper, self._next_helper, self._override_eid],
            _on_change
        ))
        self._unsubs.append(async_track_time_interval(self.hass, self._tick, timedelta(seconds=60)))
        self._refresh_attrs()
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self):
        for u in self._unsubs:
            u()
        self._unsubs = []

    def _refresh_attrs(self):
        sw = self.hass.states.get(self._switch_eid)
        active = self.hass.states.get(self._active_helper)
        nxt = self.hass.states.get(self._next_helper)
        # Check if Calendar Override is enabled for this specific mode
        override_switch = self.hass.states.get(self._override_eid)
        calendar_override_on = override_switch and override_switch.state == STATE_ON

        # Determine who controls the switch
        if calendar_override_on:
            # When override is on, it's always manual
            self._controlled_by = "manual"
        elif sw and sw.state == STATE_ON and active and active.state == "active":
            # Switch is on and there's an active calendar event
            self._controlled_by = "calendar"
        else:
            self._controlled_by = "manual"

        # Next event attrs
        if nxt:
            self._next_start = nxt.attributes.get("next_start")
            self._next_end = nxt.attributes.get("next_end")
        else:
            self._next_start = self._next_end = None

        # Active event attrs (Active Started, Active End)
        if active and active.state == "active":
            self._active_start = active.attributes.get("start")
            self._active_end = active.attributes.get("end")
            # Update last_ended when event becomes inactive
        elif self._active_start:
            # Event was active but now isn't - record when it ended
            self._last_ended = dt_util.now().isoformat()
            self._active_start = None
            self._active_end = None
        else:
            self._active_start = None
            self._active_end = None

    async def _tick(self, _now):
        self._refresh_attrs()
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        st = self.hass.states.get(self._switch_eid)
        return bool(st and st.state == STATE_ON)

    @property
    def extra_state_attributes(self):
        return {
            "mode_key": self._key,
            "controlled_by": self._controlled_by,
            "active_started": self._active_start,
            "active_end": self._active_end,
            "last_ended": self._last_ended,
            "next_calendar_start": self._next_start,
            "next_calendar_end": self._next_end,
        }


class ModeEventActiveBinarySensor(BinarySensorEntity):
    """Binary sensor that shows if there's an active calendar event (regardless of override or manual control)."""
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, key: str, friendly: str, is_custom: bool = False):
        self.hass = hass
        self._entry = entry
        self._key = key
        self._friendly_name = friendly
        self._is_custom = is_custom
        self._attr_name = f"{friendly} Event Active"
        self._attr_unique_id = f"{entry.entry_id}_{key}_event_active"
        self._active_helper = f"sensor.{DOMAIN}_active_{key}_event"
        self._override_eid = None  # Will be resolved dynamically from registry
        self._switch_eid = None  # Will be resolved dynamically from registry
        self._unsubs = []

    @property
    def device_info(self):
        # For custom calendars, use the custom name directly
        if self._is_custom:
            return {
                "identifiers": {(DOMAIN, self._entry.entry_id, self._key)},
                "manufacturer": "OnOff Automations",
                "name": self._friendly_name,
                "model": "Mode",
            }
        return device_info_for_mode(self._entry.entry_id, self._key)

    async def async_added_to_hass(self):
        # Resolve actual entity_ids from registry based on unique_ids
        registry = er.async_get(self.hass)

        # Find the mode switch entity by its unique_id
        switch_unique_id = f"{self._entry.entry_id}_{self._key}"
        for entity_id, entry in registry.entities.items():
            if entry.unique_id == switch_unique_id:
                self._switch_eid = entity_id
                break

        # Find the calendar override switch entity by its unique_id
        override_unique_id = f"{self._entry.entry_id}_{self._key}_calendar_override"
        for entity_id, entry in registry.entities.items():
            if entry.unique_id == override_unique_id:
                self._override_eid = entity_id
                break

        # Fallback to expected entity_ids if not found in registry (new entities)
        if not self._switch_eid:
            self._switch_eid = f"switch.{self._key}"
        if not self._override_eid:
            self._override_eid = f"switch.{self._key}_calendar_override"

        @callback
        def _on_change(_event):
            self.async_write_ha_state()
        # Track active event helper, override switch, and mode switch
        self._unsubs.append(async_track_state_change_event(
            self.hass,
            [self._active_helper, self._override_eid, self._switch_eid],
            _on_change
        ))
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self):
        for u in self._unsubs:
            u()
        self._unsubs = []

    @property
    def is_on(self) -> bool:
        """ON when there's an active calendar event (even with override), but NOT when manually turned on."""
        active = self.hass.states.get(self._active_helper)
        sw = self.hass.states.get(self._switch_eid)

        # If there's an active calendar event, we're ON
        if active and active.state == "active":
            return True

        # If the switch is manually on (no active calendar event), we're OFF
        # This differentiates between calendar-triggered and manual activation
        return False

    @property
    def extra_state_attributes(self):
        active = self.hass.states.get(self._active_helper)
        override_switch = self.hass.states.get(self._override_eid)
        override_on = override_switch and override_switch.state == STATE_ON

        attrs = {
            "mode_key": self._key,
            "calendar_override_enabled": override_on,
        }

        # Add calendar event start/end times if there's an active event
        if active and active.state == "active":
            attrs["event_start"] = active.attributes.get("start")
            attrs["event_end"] = active.attributes.get("end")
            attrs["event_summary"] = active.attributes.get("summary")

        return attrs


class EventModesSummaryBinarySensor(BinarySensorEntity):
    _attr_name = "Shul Modes · Event Modes"
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, event_keys: List[str]):
        self.hass = hass
        self._entry = entry
        self._event_keys = event_keys
        self._attr_unique_id = f"{entry.entry_id}_event_modes"
        self._unsub = None
        self._switch_entity_ids = {}  # Maps mode_key to actual switch entity_id

    @property
    def device_info(self):
        return device_info_main(self._entry.entry_id)

    async def async_added_to_hass(self):
        # Resolve actual entity_ids from registry
        registry = er.async_get(self.hass)

        for key in self._event_keys:
            switch_unique_id = f"{self._entry.entry_id}_{key}"
            found = False
            for entity_id, entry in registry.entities.items():
                if entry.unique_id == switch_unique_id:
                    self._switch_entity_ids[key] = entity_id
                    found = True
                    break
            # Fallback if not in registry
            if not found:
                self._switch_entity_ids[key] = f"switch.{key}"

        @callback
        def _on_change(_ev):
            self.async_write_ha_state()

        sw_ids = list(self._switch_entity_ids.values())
        self._unsub = async_track_state_change_event(self.hass, sw_ids, _on_change)
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self):
        if self._unsub:
            self._unsub()
            self._unsub = None

    @property
    def is_on(self) -> bool:
        return any(
            (state := self.hass.states.get(self._switch_entity_ids.get(k, f"switch.{k}"))) and state.state == "on"
            for k in self._event_keys
        )

    @property
    def extra_state_attributes(self):
        active = [
            k
            for k in self._event_keys
            if (state := self.hass.states.get(self._switch_entity_ids.get(k, f"switch.{k}")))
            and state.state == "on"
        ]
        return {"active_event_modes": active}


class EventRunningWithOverrideBinarySensor(BinarySensorEntity):
    """Binary sensor that is ON when any mode is running AND its calendar override is enabled."""
    _attr_name = "Shul Modes · Event Running with Calendar Override"
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, mode_keys: List[str]):
        self.hass = hass
        self._entry = entry
        self._mode_keys = mode_keys
        self._attr_unique_id = f"{entry.entry_id}_event_running_with_override"
        self._unsub = None
        self._switch_entity_ids = {}  # Maps mode_key to actual switch entity_id
        self._override_entity_ids = {}  # Maps mode_key to actual override entity_id

    @property
    def device_info(self):
        return device_info_main(self._entry.entry_id)

    async def async_added_to_hass(self):
        # Resolve actual entity_ids from registry
        registry = er.async_get(self.hass)

        for key in self._mode_keys:
            # Resolve mode switch entity_id
            switch_unique_id = f"{self._entry.entry_id}_{key}"
            found_switch = False
            for entity_id, entry in registry.entities.items():
                if entry.unique_id == switch_unique_id:
                    self._switch_entity_ids[key] = entity_id
                    found_switch = True
                    break
            if not found_switch:
                self._switch_entity_ids[key] = f"switch.{key}"

            # Resolve override switch entity_id
            override_unique_id = f"{self._entry.entry_id}_{key}_calendar_override"
            found_override = False
            for entity_id, entry in registry.entities.items():
                if entry.unique_id == override_unique_id:
                    self._override_entity_ids[key] = entity_id
                    found_override = True
                    break
            if not found_override:
                self._override_entity_ids[key] = f"switch.{key}_calendar_override"

        @callback
        def _on_change(_ev):
            self.async_write_ha_state()

        # Track all mode switches and their calendar override switches
        entity_ids = list(self._switch_entity_ids.values()) + list(self._override_entity_ids.values())
        self._unsub = async_track_state_change_event(self.hass, entity_ids, _on_change)
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self):
        if self._unsub:
            self._unsub()
            self._unsub = None

    @property
    def is_on(self) -> bool:
        """Check if any mode is ON AND its calendar override is also ON."""
        for k in self._mode_keys:
            mode_switch = self.hass.states.get(self._switch_entity_ids.get(k, f"switch.{k}"))
            override_switch = self.hass.states.get(self._override_entity_ids.get(k, f"switch.{k}_calendar_override"))

            # Check if both the mode and its override are ON
            if (mode_switch and mode_switch.state == STATE_ON and
                override_switch and override_switch.state == STATE_ON):
                return True
        return False

    @property
    def extra_state_attributes(self):
        """List all modes that are running with calendar override enabled."""
        modes_with_override = []
        for k in self._mode_keys:
            mode_switch = self.hass.states.get(self._switch_entity_ids.get(k, f"switch.{k}"))
            override_switch = self.hass.states.get(self._override_entity_ids.get(k, f"switch.{k}_calendar_override"))

            if (mode_switch and mode_switch.state == STATE_ON and
                override_switch and override_switch.state == STATE_ON):
                modes_with_override.append(k)

        return {
            "modes_with_override": modes_with_override,
            "description": "Shows when any mode is running AND its calendar override is enabled"
        }


class DSTBinarySensor(BinarySensorEntity):
    _attr_name = "Shul Modes · DST"
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_dst"

    @property
    def device_info(self):
        return device_info_main(self._entry.entry_id)

    @property
    def is_on(self) -> bool:
        now = dt_util.now()
        tz = now.tzinfo
        return bool(tz.dst(now))  # type: ignore[arg-type]

    @staticmethod
    def _next_dst_transition():
        now = dt_util.now()
        tz = now.tzinfo
        cur = tz.dst(now)  # type: ignore[arg-type]
        t = now
        limit = now + timedelta(days=730)
        while t < limit:
            nxt = t + timedelta(hours=6)
            if tz.dst(nxt) != cur:  # type: ignore[arg-type]
                lo, hi = t, nxt
                while (hi - lo) > timedelta(minutes=60):
                    mid = lo + (hi - lo) / 2
                    if tz.dst(mid) == cur:  # type: ignore[arg-type]
                        lo = mid
                    else:
                        hi = mid
                while (hi - lo) > timedelta(minutes=1):
                    mid = lo + (hi - lo) / 2
                    if tz.dst(mid) == cur:  # type: ignore[arg-type]
                        lo = mid
                    else:
                        hi = mid
                return hi.replace(second=0, microsecond=0).isoformat()
            t = nxt
        return None

    @property
    def extra_state_attributes(self):
        return {"in_dst": self.is_on, "next_change": self._next_dst_transition()}
