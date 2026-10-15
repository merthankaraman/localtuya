"""Platform to present any Tuya DP as a sensor."""
import logging
from functools import partial

import voluptuous as vol
from homeassistant.components.sensor import DEVICE_CLASSES, DOMAIN
from homeassistant.const import (
    CONF_DEVICE_CLASS,
    CONF_UNIT_OF_MEASUREMENT,
    STATE_UNKNOWN,
)

from .common import LocalTuyaEntity, async_setup_entry
from .const import (
    ATTR_STATE,
    CONF_IS_ADD_ELE,
    CONF_SCALING,
    CONF_SENSOR_MODE,
    SENSOR_MODE_ENERJI,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_PRECISION = 3


def flow_schema(dps):
    """Return schema used in config flow."""
    return {
        vol.Optional(CONF_UNIT_OF_MEASUREMENT): str,
        vol.Optional(CONF_DEVICE_CLASS): vol.In(DEVICE_CLASSES),
        vol.Optional(CONF_SCALING): vol.All(
            vol.Coerce(float), vol.Range(min=-1000000.0, max=1000000.0)
        ),
        vol.Optional(CONF_IS_ADD_ELE, default=False): bool,
    }


class LocaltuyaSensor(LocalTuyaEntity):
    """Representation of a Tuya sensor."""

    def __init__(
        self,
        device,
        config_entry,
        sensorid,
        **kwargs,
    ):
        """Initialize the Tuya sensor."""
        super().__init__(device, config_entry, sensorid, _LOGGER, **kwargs)
        self._state = STATE_UNKNOWN
        self._total = 0.0
        self._last_raw = None
        self._restored = False

    @property
    def _is_energy_mode(self):
        """Return True if this sensor accumulates add_ele pulses."""
        if CONF_IS_ADD_ELE in self._config:
            return bool(self._config.get(CONF_IS_ADD_ELE))
        # Backward compatibility with earlier sensor_mode=enerji configs
        return self._config.get(CONF_SENSOR_MODE) == SENSOR_MODE_ENERJI

    @property
    def state(self):
        """Return sensor state."""
        return self._state

    @property
    def device_class(self):
        """Return the class of this device."""
        if self._is_energy_mode:
            configured = self._config.get(CONF_DEVICE_CLASS)
            return configured or "energy"
        return self._config.get(CONF_DEVICE_CLASS)

    @property
    def state_class(self):
        """Return state class for energy dashboard compatibility."""
        if self._is_energy_mode:
            return "total_increasing"
        return None

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement of this entity, if any."""
        if self._is_energy_mode:
            return self._config.get(CONF_UNIT_OF_MEASUREMENT) or "kWh"
        return self._config.get(CONF_UNIT_OF_MEASUREMENT)

    @property
    def available(self):
        """Return if entity is available."""
        if self._is_energy_mode and self._restored:
            return True
        return super().available

    def status_restored(self, stored_state):
        """Restore accumulated energy after HA restart."""
        super().status_restored(stored_state)
        if not self._is_energy_mode:
            return

        raw_state = stored_state.attributes.get(ATTR_STATE)
        if raw_state is None and stored_state.state not in (None, STATE_UNKNOWN, "unknown"):
            raw_state = stored_state.state

        if raw_state is None:
            return

        try:
            self._total = float(raw_state)
            self._state = round(self._total, DEFAULT_PRECISION)
            self._restored = True
            self.debug(
                "Restored energy total for %s: %s", self.name, self._state
            )
        except (TypeError, ValueError):
            self.warning(
                "Could not restore energy state for %s from %s",
                self.name,
                raw_state,
            )

    def status_updated(self):
        """Device status was updated."""
        state = self.dps(self._dp_id)
        scale_factor = self._config.get(CONF_SCALING)

        if self._is_energy_mode:
            self._update_enerji(state, scale_factor)
            return

        if scale_factor is not None and isinstance(state, (int, float)):
            state = round(state * scale_factor, DEFAULT_PRECISION)
        self._state = state

    def _scaled_value(self, raw, scale_factor):
        """Apply optional scaling to a numeric DP value."""
        if raw is None or not isinstance(raw, (int, float)):
            return None
        if scale_factor is not None:
            return raw * scale_factor
        return float(raw)

    def _update_enerji(self, raw, scale_factor):
        """Accumulate add_ele incremental pulses into total kWh."""
        if raw is None or not isinstance(raw, (int, float)):
            if self._restored:
                self._state = round(self._total, DEFAULT_PRECISION)
            return

        scaled = self._scaled_value(raw, scale_factor)
        if scaled is None:
            return

        if scaled > 0 and raw != self._last_raw:
            self._total += scaled

        self._last_raw = raw
        self._total = float(self._total)
        self._state = round(self._total, DEFAULT_PRECISION)
        self._restored = True
        if not self._device.is_connecting:
            self._last_state = self._state

    def async_reset_energy(self):
        """Reset accumulated energy total to zero."""
        if not self._is_energy_mode:
            return False
        self._total = 0.0
        self._last_raw = None
        self._state = 0.0
        self._last_state = 0.0
        self._restored = True
        self.schedule_update_ha_state()
        self.debug("Energy total reset for %s", self.name)
        return True

    # No need to push restored value back to the device for a sensor
    async def restore_state_when_connected(self):
        """Do nothing for a sensor."""
        return


async_setup_entry = partial(async_setup_entry, DOMAIN, LocaltuyaSensor, flow_schema)
