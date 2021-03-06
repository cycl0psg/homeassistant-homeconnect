"""API for Home Connect bound to HASS OAuth."""

from asyncio import run_coroutine_threadsafe
import logging
import re

import homeconnect
from homeconnect.api import HomeConnectError
import voluptuous as vol

from homeassistant import config_entries, core
from homeassistant.core import callback
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.entity import Entity
from homeassistant.helpers import config_validation as cv
from homeassistant.components.sensor import DEVICE_CLASS_TEMPERATURE
from homeassistant.components.binary_sensor import DEVICE_CLASS_DOOR

from .const import (
    DOMAIN,
    SERVICE_STARTPROGRAM,
    SERVICE_STOPPROGRAM,
    PROGRAM_NAMES,
    PROGRAM_OPTIONS,
)

_LOGGER = logging.getLogger(__name__)

class ConfigEntryAuth(homeconnect.HomeConnectAPI):
    """Provide Home Connect authentication tied to an OAuth2 based config entry."""

    def __init__(
        self,
        hass: core.HomeAssistant,
        config_entry: config_entries.ConfigEntry,
        implementation: config_entry_oauth2_flow.AbstractOAuth2Implementation,
    ):
        """Initialize Home Connect Auth."""
        self.hass = hass
        self.config_entry = config_entry
        self.session = config_entry_oauth2_flow.OAuth2Session(
            hass, config_entry, implementation
        )
        super().__init__(self.session.token)
        self.devices = []

    def refresh_tokens(self) -> dict:
        """Refresh and return new Home Connect tokens using Home Assistant OAuth2 session."""
        run_coroutine_threadsafe(
            self.session.async_ensure_token_valid(), self.hass.loop
        ).result()

        return self.session.token

    def get_devices(self):
        """Get a dictionary of devices."""
        appl = self.get_appliances()
        devices = []
        for app in appl:
            if app.type == "Dryer":
                device = Dryer(app)
            elif app.type == "Washer":
                device = Washer(app)
            elif app.type == "Dishwasher":
                device = Dishwasher(app)
            elif app.type == "FridgeFreezer":
                device = FridgeFreezer(app)
            elif app.type == "Oven":
                device = Oven(app)
            elif app.type == "CoffeeMaker":
                device = CoffeeMaker(app)
            elif app.type == "Hood":
                device = Hood(app)
            elif app.type == "Hob":
                device = Hob(app)
            else:
                _LOGGER.warning("Appliance type %s not implemented.", app.type)
                continue
            devices.append({"device": device, "entities": device.get_entities()})
        self.devices = devices
        return devices


class HomeConnectDevice:
    """Generic Home Connect device."""

    # for some devices, this is instead 'BSH.Common.EnumType.PowerState.Standby'
    # see https://developer.home-connect.com/docs/settings/power_state
    power_off_state = "BSH.Common.EnumType.PowerState.Off"
    has_programs = False

    def __init__(self, appliance):
        """Initialize the device class."""
        self.appliance = appliance
        self.entities = []

    def initialize(self):
        """Fetch the info needed to initialize the device."""
        try:
            self.appliance.get_status()
        except (HomeConnectError, ValueError):
            _LOGGER.debug("Unable to fetch appliance status. Probably offline.")
        try:
            self.appliance.get_settings()
        except (HomeConnectError, ValueError):
            _LOGGER.debug("Unable to fetch settings. Probably offline.")
        try:
            program_active = self.appliance.get_programs_active()
        except (HomeConnectError, ValueError):
            _LOGGER.debug("Unable to fetch active programs. Probably offline.")
            program_active = None
        if program_active and "key" in program_active:
            self.appliance.status["BSH.Common.Root.ActiveProgram"] = {
                "value": program_active["key"]
            }
        self.appliance.listen_events(callback=self.event_callback)

    def event_callback(self, appliance):
        """Handle event."""
        _LOGGER.debug("Update triggered on %s", appliance.name)
        _LOGGER.debug(self.entities)
        _LOGGER.debug(self.appliance.status)
        for entity in self.entities:
            entity.async_entity_update()


class HomeConnectEntity(Entity):
    """Generic Home Connect entity (base class)."""

    def __init__(self, device, name):
        """Initialize the entity."""
        self.device = device
        self._name = name

    @property
    def should_poll(self):
        """No polling needed."""
        return False

    @property
    def name(self):
        """Return the name of the node (used for Entity_ID)."""
        return self._name

    @property
    def unique_id(self):
        """Return the unique id base on the id returned by Home Connect and the entity name."""
        return f"{self.device.appliance.haId}-{self.name}"

    @property
    def device_info(self):
        """Return info about the device."""
        return {
            "identifiers": {(DOMAIN, self.device.appliance.haId)},
            "name": self.device.appliance.name,
            "manufacturer": self.device.appliance.brand,
            "model": self.device.appliance.vib,
        }

    @callback
    def async_entity_update(self):
        """Update the entity."""
        _LOGGER.debug("Entity update triggered on %s", self)
        self.async_schedule_update_ha_state(True)

def convert_to_snake(camel):
    """Convert from CamelCase to snake_case.

    Taken from https://stackoverflow.com/questions/1175208/elegant-python-function-to-convert-camelcase-to-snake-case
    """
    snake = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", camel)
    return re.sub("([a-z0-9])([A-Z])", r"\1_\2", snake).lower()


def format_key(key):
    """Format Home Connect keys like `BSH.Something.SomeValue` to a simple `some_value`."""
    if not isinstance(key, str):
        return key
    return convert_to_snake(key.split(".")[-1])


class DeviceWithPrograms(HomeConnectDevice):
    """Device with programs."""

    has_programs = True

    def get_programs_available(self):
        """Get the available programs."""
        programs = self.appliance.get_programs_available()
        #_LOGGER.debug("available programs: {}".format(programs))
        return [{"name": p} for p in programs]

    def get_program_switches(self):
        """Get a dictionary with info about program switches.

        There will be one switch for each program.
        """
        programs = self.get_programs_available()
        return [{"device": self, "program_name": p['name']} for p in programs]

    def get_programs_services(self):
        """Get a dictionary with info about program services."""

        def start_program(service):
            """Service to change current home schedule."""
            program = ''
            for p_key, p_name in PROGRAM_NAMES.items():
                if p_name == service.service.replace(self.appliance.type.lower() + '_' + SERVICE_STARTPROGRAM + '_', ''):
                    program = p_key
                    options = {}
                    for d in service.data:
                        #_LOGGER.debug(f"d: {d}")
                        for o_key, o_name in PROGRAM_OPTIONS.items():
                            if o_name == d:
                                options[o_key] = service.data.get(d)
                    break
            _LOGGER.info(f"Starting progra {program} with options {options}")
            self.appliance.start_program(program, options)

        def stop_program(service):
            """Service to stop the current active program."""
            self.appliance.stop_program()
            _LOGGER.debug("stop program service called")

        programs = self.appliance.get_programs_available()
        #_LOGGER.debug("available programs: {}".format(programs))
        _services = []
        for p in programs:
            options = self.appliance.get_program_options(p)
            if options is not None:
                #_LOGGER.debug(f"program {p}")
                #_LOGGER.debug(f"options: {options}")
                _schema = {}
                i = 0
                for o in options:
                    #_LOGGER.debug(f"paramters: {o}")
                    for v in o.keys():
                        #_LOGGER.debug(f"v {v}")
                        values = options[i][v]
                        #_LOGGER.debug(f"values: {values}")
                        param_key = values['key']
                        if param_key in PROGRAM_OPTIONS:
                            param_key = PROGRAM_OPTIONS[param_key]
                        if values['type'] in ["Int", "Double"]:
                            _schema[vol.Required(param_key)] = cv.positive_int
                        elif values['type'] in ["Boolean"]:
                            _schema[vol.Required(param_key)] = cv.boolean
                        else: 
                            _LOGGER.error(f"option type {values['type']} not handled")
                    i+=1
                _service_schema = vol.Schema(_schema)
                if p in PROGRAM_NAMES:
                    program_name = PROGRAM_NAMES[p]
                else:
                    program_name = format_key(p)
                _services.append({  'service_domain': DOMAIN,
                                'service_name': self.appliance.type.lower() + '_' + SERVICE_STARTPROGRAM + '_' + program_name,
                                'service_callback': start_program,
                                'service_schema': _service_schema,
                            })
            else:
                _LOGGER.debug(f"no options found for {p}")
        _services.append({  'service_domain': DOMAIN,
                        'service_name': self.appliance.type.lower() + '_' + SERVICE_STOPPROGRAM,
                        'service_callback': stop_program,
                        'service_schema': vol.Schema({}),
                    })
        return _services


    def get_program_sensors(self):
        """Get a dictionary with info about program sensors.

        There will be one of the four types of sensors or each
        device.
        """
        sensors = {
            "Remaining Program Time": "s",
            "Elapsed Program Time": "s",
            "Duration": "s",
            "Program Progress": "%",
        }
        return [
            {
                "device": self,
                "name": " ".join((self.appliance.name, name)),
                "unit": unit,
                "key": "BSH.Common.Option.{}".format(name.replace(" ", "")),
            }
            for name, unit in sensors.items()
        ]


class DeviceWithDoor(HomeConnectDevice):
    """Device that has a door sensor."""

    def get_door_entity(self):
        """Get a dictionary with info about the door binary sensor."""
        return {
            "device": self,
            "name": self.appliance.name + " Door",
            "key": "BSH.Common.Status.DoorState",
            "device_class": DEVICE_CLASS_DOOR,
        }


class DeviceWithCustomSensors(HomeConnectDevice):
    """Device that has custom specific sensors."""

    _appliance_binary_sensors = []
    _appliance_sensors = []

    def get_appliance_sensors(self):
        """Get a dictionary with info about appliance sensors."""

        if not self.appliance.status:
            _status = self.appliance.get_status()
        else:
            _status = self.appliance.status

        _sensors = []
        for name, object_class, device_class in self._appliance_sensors:
            if object_class in _status:
                _unit = ""
                if 'unit' in _status[object_class]:
                    _unit = _status[object_class]['unit']

                _sensors.append(
                    {
                        "device": self,
                        "name": " ".join((self.appliance.name, name)),
                        "unit": _unit,
                        "key": object_class,
                        "device_class": device_class,
                    }
                )

        #_LOGGER.debug(f"sensors to be created: {_sensors}")

        _binary_sensors = []
        for name, object_class, device_class in self._appliance_binary_sensors:
            if object_class in _status:
                _binary_sensors.append(
                    {
                        "device": self,
                        "name": " ".join((self.appliance.name, name)),
                        "key": object_class,
                        "device_class": device_class,
                    }
                )

        #_LOGGER.debug(f"binary sensors to be created: {_binary_sensors}")
        return _sensors, _binary_sensors


class Dryer(DeviceWithDoor, DeviceWithPrograms):
    """Dryer class."""

    def get_entities(self):
        """Get a dictionary with infos about the associated entities."""
        door_entity = self.get_door_entity()
        program_sensors = self.get_program_sensors()
        program_switches = self.get_program_switches()
        return {
            "binary_sensor": [door_entity],
            "switch": program_switches,
            "sensor": program_sensors,
        }


class Dishwasher(DeviceWithDoor, DeviceWithPrograms):
    """Dishwasher class."""

    def get_entities(self):
        """Get a dictionary with infos about the associated entities."""
        door_entity = self.get_door_entity()
        program_sensors = self.get_program_sensors()
        program_switches = self.get_program_switches()
        return {
            "binary_sensor": [door_entity],
            "switch": program_switches,
            "sensor": program_sensors,
        }


class Oven(DeviceWithDoor, DeviceWithPrograms, DeviceWithCustomSensors):
    """Oven class."""

    power_off_state = "BSH.Common.EnumType.PowerState.Standby"

    _appliance_binary_sensors = [
        [ "Local Control Active", "BSH.Common.Status.LocalControlActive", None ],
        [ "Remote Control Start Allowed", "BSH.Common.Status.RemoteControlStartAllowed", None ],
        [ "Remote Control Active", "BSH.Common.Status.RemoteControlActive", None ],
        [ "Fast PreHeat", "Cooking.Oven.Option.FastPreHeat", None ],
    ]

    _appliance_sensors = [
        [ "Current Cavity Temperature", "Cooking.Oven.Status.CurrentCavityTemperature", DEVICE_CLASS_TEMPERATURE ],
        [ "Operation State", "BSH.Common.Status.OperationState", None ],
        [ "Power State", "BSH.Common.Setting.PowerState", None ],
        [ "Setpoint Temperature", "Cooking.Oven.Option.SetpointTemperature", DEVICE_CLASS_TEMPERATURE ],
        [ "Active Program", "BSH.Common.Root.ActiveProgram", None ],
    ]

    def get_entities(self):
        """Get a dictionary with infos about the associated entities."""
        door_entity = self.get_door_entity()
        _sensors, _binary_sensors = self.get_appliance_sensors()
        binary_sensors = [door_entity] + _binary_sensors
        sensors = self.get_program_sensors() + _sensors
        switches = self.get_program_switches()
        self.get_programs_services()
        return {
            "binary_sensor": binary_sensors,
            "switch": switches,
            "sensor": sensors,
        }


class Washer(DeviceWithDoor, DeviceWithPrograms):
    """Washer class."""

    def get_entities(self):
        """Get a dictionary with infos about the associated entities."""
        door_entity = self.get_door_entity()
        program_sensors = self.get_program_sensors()
        program_switches = self.get_program_switches()
        return {
            "binary_sensor": [door_entity],
            "switch": program_switches,
            "sensor": program_sensors,
        }


class CoffeeMaker(DeviceWithPrograms):
    """Coffee maker class."""

    power_off_state = "BSH.Common.EnumType.PowerState.Standby"

    def get_entities(self):
        """Get a dictionary with infos about the associated entities."""
        program_sensors = self.get_program_sensors()
        program_switches = self.get_program_switches()
        return {"switch": program_switches, "sensor": program_sensors}


class Hood(DeviceWithPrograms):
    """Hood class."""

    def get_entities(self):
        """Get a dictionary with infos about the associated entities."""
        program_sensors = self.get_program_sensors()
        program_switches = self.get_program_switches()
        return {"switch": program_switches, "sensor": program_sensors}


class FridgeFreezer(DeviceWithDoor):
    """Fridge/Freezer class."""

    def get_entities(self):
        """Get a dictionary with infos about the associated entities."""
        door_entity = self.get_door_entity()
        return {"binary_sensor": [door_entity]}


class Hob(DeviceWithPrograms):
    """Hob class."""

    def get_entities(self):
        """Get a dictionary with infos about the associated entities."""
        program_sensors = self.get_program_sensors()
        program_switches = self.get_program_switches()
        return {"switch": program_switches, "sensor": program_sensors}
