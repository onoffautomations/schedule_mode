from __future__ import annotations
import logging
from datetime import timedelta
from typing import Dict, List, Any
from homeassistant.components.sensor import SensorEntity, RestoreSensor
from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers import entity_registry as er

from .const import (
    DOMAIN, SIGNAL_EVENTS_UPDATED, device_info_for_mode, device_info_main,
    OPT_CUSTOM_CALENDARS, normalize_custom_calendars
)

_LOGGER = logging.getLogger(__name__)


async def _cleanup_orphaned_event_sensors(hass: HomeAssistant, entry: ConfigEntry):
    """Remove orphaned event sensors from entity registry that are unavailable or no longer provided."""
    from homeassistant.helpers.storage import Store

    # Load current events to check if sensors have corresponding events
    store = Store(hass, 1, f"{DOMAIN}_{entry.entry_id}_events.json")
    data = await store.async_load() or {}
    current_events = data.get("events", [])
    current_event_uids = {ev.get("uid") for ev in current_events if ev.get("uid")}

    registry = er.async_get(hass)
    all_entities = er.async_entries_for_config_entry(registry, entry.entry_id)

    removed_count = 0
    for entity_entry in all_entities:
        # Only look at sensor domain
        if entity_entry.domain != "sensor":
            continue

        # Check if it's an event sensor (unique_id contains "_event_")
        if "_event_" not in entity_entry.unique_id:
            continue

        # Skip the "old_events" sensor
        if entity_entry.unique_id.endswith("_old_events"):
            continue

        # Extract event UID from unique_id (format: {entry_id}_event_{uid})
        parts = entity_entry.unique_id.split("_event_")
        event_uid = parts[1] if len(parts) == 2 else None

        # Check if this sensor's event still exists
        event_exists = event_uid in current_event_uids if event_uid else False

        # Check if this sensor is unavailable or doesn't have a state
        state = hass.states.get(entity_entry.entity_id)
        should_remove = False

        if state is None:
            # Sensor has no state - it's orphaned
            _LOGGER.info("Removing orphaned event sensor (no state): %s", entity_entry.entity_id)
            should_remove = True
        elif not event_exists:
            # Event no longer exists - remove sensor immediately regardless of state
            _LOGGER.info("Removing event sensor for deleted event (uid=%s): %s", event_uid, entity_entry.entity_id)
            should_remove = True
        elif state.state in (STATE_UNAVAILABLE, "unavailable"):
            # Event exists but sensor is unavailable - give short grace period
            last_changed = state.last_changed
            if last_changed:
                time_unavailable = (dt_util.now() - last_changed).total_seconds()
                if time_unavailable > 30:  # 30 second grace period for temporary glitches
                    _LOGGER.info("Removing event sensor unavailable for %d seconds: %s",
                                time_unavailable, entity_entry.entity_id)
                    should_remove = True

        if should_remove:
            registry.async_remove(entity_entry.entity_id)
            removed_count += 1

    if removed_count > 0:
        _LOGGER.info("Cleaned up %d orphaned/unavailable event sensors", removed_count)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    mgr = _Mgr(hass, entry, async_add_entities)
    await mgr.async_setup()

    # Immediately clean up orphaned/unavailable event sensors on startup
    await _cleanup_orphaned_event_sensors(hass, entry)


class _Mgr:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, add):
        self.hass = hass
        self.entry = entry
        self.add = add
        self.ents: Dict[str, _EventSensor] = {}
        self.unsub = None
        self.unsub_tick = None
        self.old = _OldEventsSensor(entry)  # single sink sensor

    async def async_setup(self):
        self.add([self.old])  # create the sink
        self.unsub = async_dispatcher_connect(self.hass, SIGNAL_EVENTS_UPDATED, self._on_events)
        # Sync every second for immediate cleanup and state updates
        self.unsub_tick = async_track_time_interval(self.hass, self._tick, timedelta(seconds=1))

        # Load existing events and create sensors for them at startup
        await self._load_initial_events()

    async def _tick(self, _now):
        # remove finished event sensors; add them to Old Events
        now = dt_util.now()

        # Also check for orphaned event sensors in entity registry that aren't in our tracking
        await self._cleanup_orphaned_sensors()

        for eid, ent in list(self.ents.items()):
            should_remove = False

            # Check if sensor is unavailable and remove it
            if ent.entity_id:
                state = self.hass.states.get(ent.entity_id)
                # Remove if state doesn't exist (sensor not registered) or is unavailable
                if state is None:
                    _LOGGER.info("Removing event sensor with no state: %s (entity_id=%s)", ent.name, ent.entity_id)
                    should_remove = True
                elif state.state in (STATE_UNAVAILABLE, "unavailable"):
                    _LOGGER.info("Removing unavailable event sensor: %s (state=%s, entity_id=%s)", ent.name, state.state, ent.entity_id)
                    should_remove = True

            # Check if sensor has finished (ended more than 1 day ago)
            if not should_remove and ent.has_finished(now):
                _LOGGER.debug("Removing finished event sensor: %s", ent.name)
                should_remove = True

            if should_remove:
                self.old.add_old_event(ent.event_payload())
                await ent.async_remove()
                self.ents.pop(eid, None)
            else:
                ent.async_write_ha_state()
        self.old.async_write_ha_state()

    async def _load_initial_events(self):
        """Load events from storage and create sensors for them at startup."""
        from homeassistant.helpers.storage import Store

        # Load events from storage
        store = Store(self.hass, 1, f"{DOMAIN}_{self.entry.entry_id}_events.json")
        data = await store.async_load() or {}
        events = data.get("events", [])

        if events:
            _LOGGER.info("Loading %d events from storage and creating sensors", len(events))
            # Trigger sensor creation by calling _on_events with loaded events
            self._on_events(self.entry.entry_id, events)

    async def _cleanup_orphaned_sensors(self):
        """Remove orphaned event sensors from entity registry that are unavailable."""
        await _cleanup_orphaned_event_sensors(self.hass, self.entry)

    @callback
    def _on_events(self, entry_id: str, events: List[Dict[str, Any]]):
        _LOGGER.debug("Sensor manager received SIGNAL_EVENTS_UPDATED: entry_id=%s, events=%d", entry_id, len(events))

        if entry_id != self.entry.entry_id:
            _LOGGER.debug("Ignoring events for different entry: %s vs %s", entry_id, self.entry.entry_id)
            return

        cur = set(self.ents.keys())
        _LOGGER.debug("Current sensors: %d", len(cur))

        incoming = set([ev["uid"] for ev in events])
        _LOGGER.debug("Incoming events: %d", len(incoming))

        new = []
        for ev in events:
            i = ev["uid"]
            if i not in self.ents:
                # Check if entity already exists in entity registry to avoid duplicates
                registry = er.async_get(self.hass)
                unique_id = f"{self.entry.entry_id}_event_{i}"
                existing = None
                for entity_id, entry in registry.entities.items():
                    if entry.unique_id == unique_id:
                        existing = entity_id
                        break

                if existing:
                    # Entity already exists in registry, skip creating duplicate
                    _LOGGER.debug("Event sensor %s already exists in registry, skipping duplicate creation", i)
                    continue

                ent = _EventSensor(self.entry, ev)
                self.ents[i] = ent
                new.append(ent)
                _LOGGER.debug("Creating new event sensor: %s (uid=%s)", ent.name, i)
            else:
                self.ents[i].update_event(ev)
                _LOGGER.debug("Updating event sensor: uid=%s", i)

        if new:
            _LOGGER.debug("Adding %d new event sensors", len(new))
            self.add(new)

        # events deleted externally → treat as finished & archive
        deleted_ids = cur - incoming
        if deleted_ids:
            _LOGGER.debug("Removing %d event sensors", len(deleted_ids))

        for rid in list(deleted_ids):
            ent = self.ents.pop(rid, None)
            if ent:
                _LOGGER.debug("Removing event sensor: %s (uid=%s)", ent.name, rid)
                self.old.add_old_event(ent.event_payload())

                # Remove the entity using async_remove (only if entity has hass set)
                if hasattr(ent, 'hass') and ent.hass is not None:
                    try:
                        async def _remove_entity():
                            try:
                                await ent.async_remove()
                                _LOGGER.debug("Successfully removed entity %s", ent.name)
                            except Exception as ex:
                                _LOGGER.debug("Could not async_remove %s: %s", ent.name, ex)

                        self.hass.async_create_task(_remove_entity())
                    except Exception as e:
                        _LOGGER.debug("Failed to schedule removal for %s: %s", ent.name, e)
                else:
                    # Entity not fully initialized, just remove from registry if it exists
                    _LOGGER.debug("Entity %s has no hass, removing from registry only", ent.name)
                    if ent.entity_id:
                        try:
                            registry = er.async_get(self.hass)
                            if registry.async_get(ent.entity_id):
                                registry.async_remove(ent.entity_id)
                        except Exception as e:
                            _LOGGER.debug("Could not remove %s from registry: %s", ent.name, e)

        _LOGGER.debug("Sensor manager update complete. Current sensors: %d", len(self.ents))
        self.old.async_write_ha_state()


class _EventSensor(RestoreSensor):
    _attr_should_poll = False
    _attr_entity_registry_enabled_default = True

    def __init__(self, entry: ConfigEntry, ev: Dict[str, Any]):
        self.entry = entry
        self._ev: Dict[str, Any] = dict(ev)
        self._attr_unique_id = f"{entry.entry_id}_event_{ev['uid']}"

    @property
    def device_info(self):
        mode_key = self._ev.get("mode_key", "event_mode")

        # Check if this is a custom calendar
        custom_calendars = normalize_custom_calendars(self.entry.options.get(OPT_CUSTOM_CALENDARS, []))
        for custom_cal in custom_calendars:
            if custom_cal["id"] == mode_key:
                # This is a custom calendar - use the formatted name
                return {
                    "identifiers": {(DOMAIN, self.entry.entry_id, mode_key)},
                    "manufacturer": "OnOff Automations",
                    "name": custom_cal["name"],
                    "model": "Mode",
                }

        # Not a custom calendar - use the standard device_info
        return device_info_for_mode(self.entry.entry_id, mode_key)

    @property
    def name(self) -> str:
        return self._ev.get("summary") or f"Event {self._ev.get('uid', '')}"

    def has_finished(self, now=None) -> bool:
        """Returns True if event ended more than 1 day ago"""
        now = now or dt_util.now()
        en = dt_util.parse_datetime(self._ev.get("end"))
        if not en:
            return False
        # Delete sensor 1 day after event ends
        return now >= (en + timedelta(days=1))

    def event_payload(self) -> Dict[str, Any]:
        return dict(self._ev)

    @property
    def state(self) -> str:
        now = dt_util.now()
        st = dt_util.parse_datetime(self._ev.get("start"))
        en = dt_util.parse_datetime(self._ev.get("end"))
        if not st or not en:
            return "unknown"
        if now < st:
            return "upcoming"
        if st <= now < en:
            return "running"
        return "ended"

    @property
    def extra_state_attributes(self):
        st = dt_util.parse_datetime(self._ev.get("start") or "")
        en = dt_util.parse_datetime(self._ev.get("end") or "")
        dur = (en - st).total_seconds() if (st and en) else None
        out = dict(self._ev)
        out["duration_seconds"] = dur
        return out

    def update_event(self, ev: Dict[str, Any]):
        self._ev = dict(ev)


class _OldEventsSensor(SensorEntity):
    """Single sensor that accumulates past events in attributes."""
    _attr_should_poll = False
    _attr_name = "Old Events"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = True

    def __init__(self, entry: ConfigEntry):
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_old_events"
        self._events: Dict[str, Dict[str, Any]] = {}

    @property
    def device_info(self):
        return device_info_main(self._entry.entry_id)

    @property
    def state(self) -> int:
        return len(self._events)

    def add_old_event(self, payload: Dict[str, Any]):
        eid = payload.get("uid") or f"e_{len(self._events)+1}"
        self._events[str(eid)] = payload

    @property
    def extra_state_attributes(self):
        # flatten each event into its own attribute key
        attrs = {"count": len(self._events)}
        for k, ev in self._events.items():
            attrs[f"event_{k}"] = ev
        return attrs
