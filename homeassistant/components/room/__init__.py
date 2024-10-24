"""Support for the definition of rooms."""

from __future__ import annotations

from collections.abc import Callable, Iterable
import logging
from operator import attrgetter
import sys
from typing import Any, Self, cast

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import (
    ATTR_EDITABLE,
    ATTR_LATITUDE,
    ATTR_LONGITUDE,
    ATTR_PERSONS,
    CONF_COMMAND,
    CONF_CONTEXT,
    CONF_COORDS,
    CONF_ICON,
    CONF_ID,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_NAME,
    CONF_RADIUS,
    CONF_RESPONSE,
    CONF_STATE,
    CONF_STATUS,
    EVENT_CORE_CONFIG_UPDATE,
    SERVICE_RELOAD,
    STATE_UNAVAILABLE,
    Platform,
)
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    ServiceCall,
    State,
    callback,
)
from homeassistant.helpers import (
    collection,
    config_validation as cv,
    entity_component,
    event,
    service,
    storage,
)
from homeassistant.helpers.entity_platform import (
    AddEntitiesCallback,
    async_get_current_platform,
)
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import bind_hass
from homeassistant.util.location import distance

from .const import (
    ATTR_COMMAND,
    ATTR_CONTEXT,
    ATTR_COORDS,
    ATTR_RADIUS,
    ATTR_RESPONSE,
    ATTR_STATE,
    ATTR_STATUS,
    DOMAIN,
    SERVICE_SET_COMMAND,
    SERVICE_SET_CONTEXT,
    SERVICE_SET_RESPONSE,
    SERVICE_SET_STATUS,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_RADIUS = 100

ENTITY_ID_FORMAT = "room.{}"

ICON_IMPORT = "mdi:import"

CREATE_FIELDS = {
    vol.Required(CONF_NAME): cv.string,
    vol.Optional(CONF_STATE, default="ok"): cv.string,
    vol.Required(CONF_LATITUDE): cv.latitude,
    vol.Required(CONF_LONGITUDE): cv.longitude,
    vol.Optional(CONF_RADIUS, default=DEFAULT_RADIUS): vol.Coerce(float),
    vol.Optional(CONF_ICON): cv.icon,
    vol.Optional(CONF_COORDS): cv.string,
    vol.Optional(CONF_STATUS): cv.string,
    vol.Optional(CONF_CONTEXT): cv.string,
    vol.Optional(CONF_COMMAND): cv.string,
    vol.Optional(CONF_RESPONSE): cv.string,
}


UPDATE_FIELDS = {
    vol.Optional(CONF_NAME): cv.string,
    vol.Optional(CONF_STATE, default="ok"): cv.string,
    vol.Optional(CONF_LATITUDE): cv.latitude,
    vol.Optional(CONF_LONGITUDE): cv.longitude,
    vol.Optional(CONF_RADIUS): vol.Coerce(float),
    vol.Optional(CONF_ICON): cv.icon,
    vol.Optional(CONF_COORDS): cv.string,
    vol.Optional(CONF_STATUS): cv.string,
    vol.Optional(CONF_CONTEXT): cv.string,
    vol.Optional(CONF_COMMAND): cv.string,
    vol.Optional(CONF_RESPONSE): cv.string,
}


def empty_value(value: Any) -> Any:
    """Test if the user has the default config value from adding "room:"."""
    if isinstance(value, dict) and len(value) == 0:
        return []

    raise vol.Invalid("Not a default value")


CONFIG_SCHEMA = vol.Schema(
    {
        vol.Optional(DOMAIN, default=[]): vol.Any(
            vol.All(cv.ensure_list, [vol.Schema(CREATE_FIELDS)]),
            empty_value,
        )
    },
    extra=vol.ALLOW_EXTRA,
)

PLATFORMS = [
    Platform.SELECT,
]

RELOAD_SERVICE_SCHEMA = vol.Schema({})
SET_STATUS_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("status"): cv.string,
    }
)
STORAGE_KEY = DOMAIN
STORAGE_VERSION = 1

ENTITY_ID_SORTER = attrgetter("entity_id")

ROOM_ENTITY_IDS = "room_entity_ids"


@bind_hass
def async_active_room(
    hass: HomeAssistant, latitude: float, longitude: float, radius: int = 0
) -> State | None:
    """Find the active room for given latitude, longitude.

    This method must be run in the event loop.
    """
    # Sort entity IDs so that we are deterministic if equal distance to 2 rooms
    min_dist: float = sys.maxsize
    closest: State | None = None

    # This can be called before async_setup by device tracker
    room_entity_ids: Iterable[str] = hass.data.get(ROOM_ENTITY_IDS, ())

    for entity_id in room_entity_ids:
        if (
            not (room := hass.states.get(entity_id))
            # Skip unavailable rooms
            or room.state == STATE_UNAVAILABLE
            # Skip rooms where we cannot calculate distance
            or (
                room_dist := distance(
                    latitude,
                    longitude,
                    room.attributes[ATTR_LATITUDE],
                    room.attributes[ATTR_LONGITUDE],
                )
            )
            is None
            # Skip room that are outside the radius aka the
            # lat/long is outside the room
            or not (room_dist - (room_radius := room.attributes[ATTR_RADIUS]) < radius)
        ):
            continue

        # If have a closest and its not closer than the closest skip it
        if closest and not (
            room_dist < min_dist
            or (
                # If same distance, prefer smaller room
                room_dist == min_dist and room_radius < closest.attributes[ATTR_RADIUS]
            )
        ):
            continue

        # We got here which means it closer than the previous known closest
        # or equal distance but this one is smaller.
        min_dist = room_dist
        closest = room

    return closest


@callback
def async_setup_track_room_entity_ids(hass: HomeAssistant) -> None:
    """Set up track of entity IDs for rooms."""
    room_entity_ids: list[str] = hass.states.async_entity_ids(DOMAIN)
    hass.data[ROOM_ENTITY_IDS] = room_entity_ids

    @callback
    def _async_add_room_entity_id(
        event_: Event[EventStateChangedData],
    ) -> None:
        """Add room entity ID."""
        room_entity_ids.append(event_.data["entity_id"])
        room_entity_ids.sort()

    @callback
    def _async_remove_room_entity_id(
        event_: Event[EventStateChangedData],
    ) -> None:
        """Remove room entity ID."""
        room_entity_ids.remove(event_.data["entity_id"])

    event.async_track_state_added_domain(hass, DOMAIN, _async_add_room_entity_id)
    event.async_track_state_removed_domain(hass, DOMAIN, _async_remove_room_entity_id)


def in_room(room: State, latitude: float, longitude: float, radius: float = 0) -> bool:
    """Test if given latitude, longitude is in given room.

    Async friendly.
    """
    if room.state == STATE_UNAVAILABLE:
        return False

    room_dist = distance(
        latitude,
        longitude,
        room.attributes[ATTR_LATITUDE],
        room.attributes[ATTR_LONGITUDE],
    )

    if room_dist is None or room.attributes[ATTR_RADIUS] is None:
        return False
    return room_dist - radius < cast(float, room.attributes[ATTR_RADIUS])


class RoomStorageCollection(collection.DictStorageCollection):
    """Room collection stored in storage."""

    CREATE_SCHEMA = vol.Schema(CREATE_FIELDS)
    UPDATE_SCHEMA = vol.Schema(UPDATE_FIELDS)

    async def _process_create_data(self, data: dict) -> dict:
        """Validate the config is valid."""
        return cast(dict, self.CREATE_SCHEMA(data))

    @callback
    def _get_suggested_id(self, info: dict) -> str:
        """Suggest an ID based on the config."""
        return cast(str, info[CONF_NAME])

    async def _update_data(self, item: dict, update_data: dict) -> dict:
        """Return a new updated data object."""
        update_data = self.UPDATE_SCHEMA(update_data)
        return {**item, **update_data}


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up configured rooms as well as Home Assistant room if necessary."""
    async_setup_track_room_entity_ids(hass)

    component = entity_component.EntityComponent[Room](_LOGGER, DOMAIN, hass)
    id_manager = collection.IDManager()

    yaml_collection = collection.IDLessCollection(
        logging.getLogger(f"{__name__}.yaml_collection"), id_manager
    )
    collection.sync_entity_lifecycle(
        hass, DOMAIN, DOMAIN, component, yaml_collection, Room
    )

    storage_collection = RoomStorageCollection(
        storage.Store(hass, STORAGE_VERSION, STORAGE_KEY),
        id_manager,
    )
    collection.sync_entity_lifecycle(
        hass, DOMAIN, DOMAIN, component, storage_collection, Room
    )

    if config[DOMAIN]:
        await yaml_collection.async_load(config[DOMAIN])

    await storage_collection.async_load()

    collection.DictStorageCollectionWebsocket(
        storage_collection, DOMAIN, DOMAIN, CREATE_FIELDS, UPDATE_FIELDS
    ).async_setup(hass)

    async def reload_service_handler(service_call: ServiceCall) -> None:
        """Remove all rooms and load new ones from config."""
        conf = await component.async_prepare_reload(skip_reset=True)
        if conf is None:
            return
        await yaml_collection.async_load(conf[DOMAIN])

    service.async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_RELOAD,
        reload_service_handler,
        schema=RELOAD_SERVICE_SCHEMA,
    )

    component.async_register_entity_service(
        SERVICE_SET_STATUS,
        {vol.Required(ATTR_STATUS): cv.string},
        "async_set_status",
    )

    async def core_config_updated(_: Event) -> None:
        """Handle core config updated."""

    hass.bus.async_listen(EVENT_CORE_CONFIG_UPDATE, core_config_updated)

    hass.data[DOMAIN] = storage_collection

    return True


async def async_setup_entry(
    hass: HomeAssistant, config_entry: config_entries.ConfigEntry
) -> bool:
    """Set up room as config entry."""
    storage_collection = cast(RoomStorageCollection, hass.data[DOMAIN])

    data = dict(config_entry.data)
    data.setdefault(CONF_RADIUS, DEFAULT_RADIUS)

    await storage_collection.async_create_item(data)

    hass.async_create_task(
        hass.config_entries.async_remove(config_entry.entry_id), eager_start=True
    )

    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)

    return True


async def async_unload_entry(
    hass: HomeAssistant, config_entry: config_entries.ConfigEntry
) -> bool:
    """Will be called once we remove it."""
    return True


class Room(collection.CollectionEntity):
    """Representation of a Room."""

    editable: bool
    _attr_should_poll = False

    def __init__(self, config: ConfigType) -> None:
        """Initialize the room."""
        self._status: str | None = None
        self._config = config
        self.editable = True
        self._attrs: dict | None = None
        self._remove_listener: Callable[[], None] | None = None
        self._persons_in_room: set[str] = set()
        self._set_attrs_from_config()

    def _set_attrs_from_config(self) -> None:
        """Set the attributes from the config."""
        config = self._config
        name: str = config[CONF_NAME]
        status: str = config.get(ATTR_STATUS, "ok")
        self._status = status
        self._attr_name = name
        self._case_folded_name = name.casefold()
        self._attr_unique_id = config.get(CONF_ID)
        self._attr_icon = config.get(CONF_ICON)

    @classmethod
    def from_storage(cls, config: ConfigType) -> Self:
        """Return entity instance initialized from storage."""
        room = cls(config)
        room.editable = True
        room._generate_attrs()  # noqa: SLF001
        return room

    @classmethod
    def from_yaml(cls, config: ConfigType) -> Self:
        """Return entity instance initialized from yaml."""
        room = cls(config)
        room.editable = False
        room._generate_attrs()  # noqa: SLF001
        return room

    @property
    def state(self) -> str | None:
        """Return the state of the element."""
        return self._status

    async def async_update_config(self, config: ConfigType) -> None:
        """Handle when the config is updated."""
        if self._config == config:
            return
        self._config = config
        self._set_attrs_from_config()
        self._generate_attrs()
        self.async_write_ha_state()

    @callback
    def _person_state_change_listener(self, evt: Event[EventStateChangedData]) -> None:
        person_entity_id = evt.data["entity_id"]
        persons_in_room = self._persons_in_room
        cur_count = len(persons_in_room)
        if self._state_is_in_room(evt.data["new_state"]):
            persons_in_room.add(person_entity_id)
        elif person_entity_id in persons_in_room:
            persons_in_room.remove(person_entity_id)

        if len(persons_in_room) != cur_count:
            self._generate_attrs()
            self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        await super().async_added_to_hass()
        person_domain = "person"  # avoid circular import
        self._persons_in_room = {
            state.entity_id
            for state in self.hass.states.async_all(person_domain)
            if self._state_is_in_room(state)
        }
        self._generate_attrs()

        self.async_on_remove(
            event.async_track_state_change_filtered(
                self.hass,
                event.TrackStates(False, set(), {person_domain}),
                self._person_state_change_listener,
            ).async_remove
        )

    @callback
    def _generate_attrs(self) -> None:
        """Generate new attrs based on config."""

        self._attr_extra_state_attributes = {
            ATTR_LATITUDE: self._config[CONF_LATITUDE],
            ATTR_LONGITUDE: self._config[CONF_LONGITUDE],
            ATTR_RADIUS: self._config[CONF_RADIUS],
            ATTR_COORDS: self._config[CONF_COORDS],
            ATTR_STATUS: self._config.get(CONF_STATUS, ""),
            ATTR_CONTEXT: self._config.get(CONF_CONTEXT, ""),
            ATTR_COMMAND: self._config.get(CONF_COMMAND, ""),
            ATTR_RESPONSE: self._config.get(CONF_RESPONSE, ""),
            ATTR_PERSONS: sorted(self._persons_in_room),
            ATTR_EDITABLE: self.editable,
        }

    @callback
    def _state_is_in_room(self, state: State | None) -> bool:
        """Return if given state is in room."""
        return state is not None and (state.state.casefold() == self._case_folded_name)

    async def async_set_status(self, status: str) -> None:
        """Set new preset mode."""
        _LOGGER.warning("Setting room status to %s" % status)
        self._status = status
        self.async_write_ha_state()
        # await self.hass.async_add_executor_job(self.alarm_trigger, code)
        # await hass.state
