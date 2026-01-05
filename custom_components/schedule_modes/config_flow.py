from __future__ import annotations
from typing import Any, Dict
import voluptuous as vol
import uuid

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv

from .const import (
    DOMAIN, ALL_MODE_KEYS, ensure_default_options, normalize_custom_calendars,
    OPT_ENABLED_MODES, OPT_DEFAULT_DURATIONS, OPT_AUTO_RESET_TIME,
    OPT_LINK_NO_TACHANUN_FOR_BRIS,  # NEW
    OPT_CUSTOM_CALENDARS,
)


class ScheduleModesConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self):
        super().__init__()
        self._pending_options: Dict[str, Any] = {}

    async def async_step_user(self, user_input: Dict[str, Any] | None = None):
        # Only allow one instance of this integration
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            enabled = list(user_input[OPT_ENABLED_MODES])
            sync_bris = user_input.get(OPT_LINK_NO_TACHANUN_FOR_BRIS, False)
            options = ensure_default_options({
                OPT_ENABLED_MODES: enabled,
                OPT_LINK_NO_TACHANUN_FOR_BRIS: sync_bris,
            })
            return self.async_create_entry(
                title="Schedule Modes",
                data={},
                options=options,
            )

        schema = vol.Schema({
            vol.Required(OPT_ENABLED_MODES, default=ALL_MODE_KEYS()): cv.multi_select({k: k for k in ALL_MODE_KEYS()}),
            vol.Optional(OPT_LINK_NO_TACHANUN_FOR_BRIS, default=False): bool,
        })
        return self.async_show_form(step_id="user", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return ScheduleModesOptionsFlow(config_entry)


class ScheduleModesOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, entry: config_entries.ConfigEntry):
        self.entry = entry
        self._pending: Dict[str, Any] = {}

    async def async_step_init(self, _=None):
        """Main menu for configuration options."""
        opts = ensure_default_options(self.entry.options or {})
        custom_cals = opts.get(OPT_CUSTOM_CALENDARS, [])

        # Build menu options
        menu_options = {
            "add_calendar": "Add a new calendar",
            "configure_modes": "Configure modes and durations",
        }

        if custom_cals:
            menu_options["remove_calendar"] = "Remove a calendar"

        return self.async_show_menu(
            step_id="init",
            menu_options=menu_options
        )

    async def async_step_add_calendar(self, user_input=None):
        """Add a new custom calendar."""
        errors = {}

        if user_input is not None:
            calendar_name = user_input.get("calendar_name", "").strip()
            if calendar_name:
                opts = ensure_default_options(self.entry.options or {})
                custom_cals = normalize_custom_calendars(opts.get(OPT_CUSTOM_CALENDARS, []))

                # Create new calendar with unique ID
                new_cal = {
                    "id": calendar_name.lower().replace(" ", "_"),
                    "name": calendar_name
                }

                # Check for duplicate IDs
                if any(cal["id"] == new_cal["id"] for cal in custom_cals):
                    errors["calendar_name"] = "Calendar with this name already exists"
                else:
                    custom_cals.append(new_cal)
                    opts[OPT_CUSTOM_CALENDARS] = custom_cals
                    # Ensure normalization is applied
                    opts = ensure_default_options(opts)
                    return self.async_create_entry(title="", data=opts)
            else:
                errors["calendar_name"] = "Calendar name cannot be empty"

        schema = vol.Schema({
            vol.Required("calendar_name"): str,
        })
        return self.async_show_form(
            step_id="add_calendar",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "info": "Enter a name for your custom calendar (e.g., 'Wedding', 'Vacation', 'Board Meeting')"
            }
        )

    async def async_step_remove_calendar(self, user_input=None):
        """Remove a custom calendar."""
        opts = ensure_default_options(self.entry.options or {})
        custom_cals = normalize_custom_calendars(opts.get(OPT_CUSTOM_CALENDARS, []))

        if user_input is not None:
            calendar_to_remove = user_input.get("calendar_to_remove")
            if calendar_to_remove:
                custom_cals = [cal for cal in custom_cals if cal["id"] != calendar_to_remove]
                opts[OPT_CUSTOM_CALENDARS] = custom_cals
                # Ensure normalization is applied
                opts = ensure_default_options(opts)
                return self.async_create_entry(title="", data=opts)

        # Build selection options
        calendar_options = {cal["id"]: cal["name"] for cal in custom_cals}

        if not calendar_options:
            # No calendars to remove, go back to menu
            return await self.async_step_init()

        schema = vol.Schema({
            vol.Required("calendar_to_remove"): vol.In(calendar_options),
        })
        return self.async_show_form(
            step_id="remove_calendar",
            data_schema=schema,
        )

    async def async_step_configure_modes(self, user_input=None):
        """Configure modes - redirect to select_modes."""
        return await self.async_step_select_modes(user_input)

    async def async_step_select_modes(self, user_input=None):
        opts = ensure_default_options(self.entry.options or {})
        enabled = opts.get(OPT_ENABLED_MODES, ALL_MODE_KEYS())
        schema = vol.Schema({
            vol.Required(OPT_ENABLED_MODES, default=enabled): cv.multi_select({k: k for k in ALL_MODE_KEYS()}),
        })
        if user_input is not None:
            self._pending = dict(opts)
            self._pending[OPT_ENABLED_MODES] = list(user_input[OPT_ENABLED_MODES])
            return await self.async_step_durations()
        return self.async_show_form(step_id="select_modes", data_schema=schema)

    async def async_step_durations(self, user_input=None):
        opts = self._pending or ensure_default_options(self.entry.options or {})
        durs = opts.get(OPT_DEFAULT_DURATIONS, {})
        fields = {}
        for k in opts.get(OPT_ENABLED_MODES, ALL_MODE_KEYS()):
            fields[vol.Optional(f"dur_{k}", default=int(durs.get(k, 0)))] = vol.Coerce(int)
        fields[vol.Optional(OPT_AUTO_RESET_TIME, default=opts.get(OPT_AUTO_RESET_TIME, ""))] = str
        # NEW: UI toggle for linking NT during Bris
        fields[vol.Optional(OPT_LINK_NO_TACHANUN_FOR_BRIS, default=opts.get(OPT_LINK_NO_TACHANUN_FOR_BRIS, False))] = bool

        if user_input is not None:
            nd = {}
            for k in opts[OPT_ENABLED_MODES]:
                nd[k] = int(user_input.get(f"dur_{k}", 0))
            opts[OPT_DEFAULT_DURATIONS] = nd
            opts[OPT_AUTO_RESET_TIME] = user_input.get(OPT_AUTO_RESET_TIME, "").strip()
            # NEW: persist link setting
            opts[OPT_LINK_NO_TACHANUN_FOR_BRIS] = bool(user_input.get(OPT_LINK_NO_TACHANUN_FOR_BRIS, False))
            return self.async_create_entry(title="", data=opts)

        return self.async_show_form(step_id="durations", data_schema=vol.Schema(fields))
