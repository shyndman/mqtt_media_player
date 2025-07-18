"""Config flow for MQTT Media Player integration."""
import asyncio
import json
import logging
from typing import Any, Dict, Optional
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.const import CONF_NAME
from homeassistant.components.mqtt import async_subscribe
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig, SelectSelectorMode
from homeassistant.exceptions import HomeAssistantError

from .const import (
    DOMAIN,
    CONFIG_TOPIC_PATTERN,
    DISCOVERY_TOPIC,
    MQTT_CONFIG_SCHEMA,
)

_LOGGER = logging.getLogger(__name__)

DISCOVERY_TIMEOUT = 5  # seconds


class MqttMediaPlayerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for MQTT Media Player."""

    VERSION = 1
    
    def __init__(self):
        """Initialize the config flow."""
        self._discovered_devices = {}
        self._selected_device = None

    async def async_step_mqtt(self, discovery_info: dict):
        """Handle MQTT discovery."""
        _LOGGER.debug("MQTT discovery triggered: %s", discovery_info)
        
        try:
            # Extract device info from discovery
            config_data = json.loads(discovery_info["payload"])
            device_name = discovery_info["topic"].split("/")[-2]
            
            # Create unique ID from device name or unique_id in config
            unique_id = config_data.get("unique_id", device_name)
            
            # Set unique ID to prevent duplicates
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()
            
            # Store discovered device
            self._discovered_devices[device_name] = {
                "name": config_data.get("name", device_name),
                "config": config_data,
                "unique_id": unique_id,
            }
            
            return await self.async_step_discovered_device()
            
        except Exception as e:
            _LOGGER.error("Error processing MQTT discovery: %s", e)
            return self.async_abort(reason="discovery_error")

    async def async_step_discovered_device(self, user_input: Optional[Dict[str, Any]] = None):
        """Handle discovered device confirmation."""
        if user_input is not None:
            device_name = list(self._discovered_devices.keys())[0]
            device_info = self._discovered_devices[device_name]
            
            return self.async_create_entry(
                title=device_info["name"],
                data={"mqtt_config": device_info["config"]},
            )
        
        device_name = list(self._discovered_devices.keys())[0]
        device_info = self._discovered_devices[device_name]
        
        return self.async_show_form(
            step_id="discovered_device",
            description_placeholders={"device_name": device_info["name"]},
            data_schema=vol.Schema({}),
        )

    async def async_step_user(self, user_input: Optional[Dict[str, Any]] = None):
        """Handle the initial step."""
        errors = {}
        
        if user_input is not None:
            if user_input["setup_mode"] == "discover":
                return await self.async_step_discovery()
            else:
                return await self.async_step_manual()
        
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("setup_mode"): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            {"value": "discover", "label": "Discover available devices"},
                            {"value": "manual", "label": "Manual configuration"},
                        ],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
            }),
            errors=errors,
        )

    async def async_step_discovery(self, user_input: Optional[Dict[str, Any]] = None):
        """Handle device discovery."""
        if user_input is not None:
            if user_input["device"] == "manual":
                return await self.async_step_manual()
            
            # Selected a discovered device
            device_name = user_input["device"]
            device_info = self._discovered_devices[device_name]
            
            # Set unique ID
            await self.async_set_unique_id(device_info["unique_id"])
            self._abort_if_unique_id_configured()
            
            return self.async_create_entry(
                title=device_info["name"],
                data={"mqtt_config": device_info["config"]},
            )
        
        # Discover available devices
        try:
            await self._discover_devices()
            
            if not self._discovered_devices:
                return await self.async_step_manual()
            
            # Prepare device options
            device_options = []
            for device_name, device_info in self._discovered_devices.items():
                device_options.append({
                    "value": device_name,
                    "label": f"{device_info['name']} ({device_name})"
                })
            
            device_options.append({
                "value": "manual",
                "label": "Manual configuration"
            })
            
            return self.async_show_form(
                step_id="discovery",
                data_schema=vol.Schema({
                    vol.Required("device"): SelectSelector(
                        SelectSelectorConfig(
                            options=device_options,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }),
                description_placeholders={"device_count": str(len(self._discovered_devices))},
            )
            
        except Exception as e:
            _LOGGER.error("Error during device discovery: %s", e)
            errors = {"base": "discovery_failed"}
            return self.async_show_form(
                step_id="discovery",
                data_schema=vol.Schema({
                    vol.Required("device"): SelectSelector(
                        SelectSelectorConfig(
                            options=[{"value": "manual", "label": "Manual configuration"}],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }),
                errors=errors,
            )

    async def async_step_manual(self, user_input: Optional[Dict[str, Any]] = None):
        """Handle manual device configuration."""
        errors = {}
        
        if user_input is not None:
            device_name = user_input[CONF_NAME]
            
            try:
                # Validate device name format
                if not device_name.replace("_", "").replace("-", "").isalnum():
                    errors[CONF_NAME] = "invalid_device_name"
                else:
                    # Try to fetch MQTT config
                    mqtt_config = await self._fetch_mqtt_config(device_name)
                    if mqtt_config:
                        # Set unique ID
                        unique_id = mqtt_config.get("unique_id", device_name)
                        await self.async_set_unique_id(unique_id)
                        self._abort_if_unique_id_configured()
                        
                        return self.async_create_entry(
                            title=mqtt_config.get("name", device_name),
                            data={"mqtt_config": mqtt_config},
                        )
                    else:
                        errors[CONF_NAME] = "device_not_found"
                        
            except Exception as e:
                _LOGGER.error("Error during manual configuration: %s", e)
                errors["base"] = "unknown"
        
        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema({
                vol.Required(CONF_NAME): str,
            }),
            errors=errors,
        )

    async def _discover_devices(self) -> None:
        """Discover available MQTT media player devices."""
        _LOGGER.debug("Starting device discovery")
        
        discovered_configs = []
        
        def discovery_callback(message):
            """Handle discovered device configs."""
            try:
                config_data = json.loads(message.payload)
                device_name = message.topic.split("/")[-2]
                
                _LOGGER.debug("Discovered device: %s", device_name)
                discovered_configs.append({
                    "device_name": device_name,
                    "config": config_data,
                })
            except Exception as e:
                _LOGGER.warning("Error parsing discovery message: %s", e)
        
        # Subscribe to discovery topic
        subscription = await async_subscribe(
            self.hass, DISCOVERY_TOPIC, discovery_callback, qos=0
        )
        
        try:
            # Wait for discovery messages
            await asyncio.sleep(DISCOVERY_TIMEOUT)
            
            # Process discovered devices
            for discovered in discovered_configs:
                device_name = discovered["device_name"]
                config_data = discovered["config"]
                unique_id = config_data.get("unique_id", device_name)
                
                # Check if already configured
                existing_entry = None
                for entry in self._async_current_entries():
                    if entry.data.get("mqtt_config", {}).get("unique_id") == unique_id:
                        existing_entry = entry
                        break
                
                if existing_entry is None:
                    self._discovered_devices[device_name] = {
                        "name": config_data.get("name", device_name),
                        "config": config_data,
                        "unique_id": unique_id,
                    }
                    
            _LOGGER.debug("Discovery complete. Found %d devices", len(self._discovered_devices))
            
        finally:
            # Unsubscribe
            subscription()

    async def _fetch_mqtt_config(self, device_name: str) -> Optional[Dict[str, Any]]:
        """Fetch MQTT config for a specific device."""
        _LOGGER.debug("Fetching MQTT config for: %s", device_name)
        
        config_topic = CONFIG_TOPIC_PATTERN.format(device_name)
        received_config = None
        
        def config_callback(message):
            """Handle config message."""
            nonlocal received_config
            try:
                received_config = json.loads(message.payload)
                _LOGGER.debug("Received config for %s: %s", device_name, received_config)
            except Exception as e:
                _LOGGER.error("Error parsing config message: %s", e)
        
        # Subscribe to config topic
        subscription = await async_subscribe(
            self.hass, config_topic, config_callback, qos=0
        )
        
        try:
            # Wait for config message
            await asyncio.sleep(2)
            
            if received_config:
                # Validate config
                validated_config = MQTT_CONFIG_SCHEMA(received_config)
                return validated_config
            else:
                _LOGGER.warning("No config received for device: %s", device_name)
                return None
                
        except vol.Invalid as e:
            _LOGGER.error("Invalid config for device %s: %s", device_name, e)
            return None
        except Exception as e:
            _LOGGER.error("Error fetching config for device %s: %s", device_name, e)
            return None
        finally:
            # Unsubscribe
            subscription()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get options flow."""
        return OptionsFlowHandler(config_entry)


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(self, user_input: Optional[Dict[str, Any]] = None):
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional("example_option", default=self.config_entry.options.get("example_option", True)): bool,
            }),
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidConfig(HomeAssistantError):
    """Error to indicate there is invalid config."""
