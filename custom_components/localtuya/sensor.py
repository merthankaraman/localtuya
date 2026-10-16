"""Platform to present any Tuya DP as a sensor."""
import logging
from functools import partial

import voluptuous as vol
from homeassistant.components.sensor import (
    DEVICE_CLASSES,
    DOMAIN,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
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


class LocaltuyaSensor(LocalTuyaEntity, SensorEntity):
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
        self._last_event_time = None
        self._restored = False

    @property
    def _is_energy_mode(self):
        """Return True if this sensor accumulates add_ele pulses."""
        if CONF_IS_ADD_ELE in self._config:
            return bool(self._config.get(CONF_IS_ADD_ELE))
        # Backward compatibility with earlier sensor_mode=enerji configs
        return self._config.get(CONF_SENSOR_MODE) == SENSOR_MODE_ENERJI

    @property
    def native_value(self):
        """Return sensor state."""
        return self._state

    @property
    def device_class(self):
        """Return the class of this device."""
        return self._config.get(CONF_DEVICE_CLASS)

    @property
    def state_class(self):
        """Add Energy-dashboard state_class from selected device_class."""
        device_class = self._config.get(CONF_DEVICE_CLASS)
        if device_class in (SensorDeviceClass.ENERGY, "energy"):
            return SensorStateClass.TOTAL_INCREASING
        if device_class in (SensorDeviceClass.POWER, "power"):
            return SensorStateClass.MEASUREMENT
        return None

    @property
    def native_unit_of_measurement(self):
        """Return the unit of measurement of this entity, if any."""
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

    def _dp_in_packet(self):
        """Return True if this sensor's DP was present in the last device message."""
        if self._updated_dps is None:
            return False
        dp = str(self._dp_id)
        return dp in {str(k) for k in self._updated_dps}

    def _should_accumulate_pulse(self, raw):
        """Decide if this add_ele report should be added to the total.

        Cloud/local plugs may repeat the same Wh increment; a new packet (or
        new device timestamp ``t``) means a new pulse even when the value is
        unchanged. Full status polls pass updated_dps=None and never accumulate.
        """
        if not self._dp_in_packet():
            return False

        event_time = self._event_time
        if event_time is not None:
            if event_time == self._last_event_time:
                return False
            return True

        # No timestamp: value change always counts
        if raw != self._last_raw:
            return True

        # Same value: only count dedicated single-DP pushes (not multi-DP polls)
        return len(self._updated_dps) == 1

    def _update_enerji(self, raw, scale_factor):
        """Accumulate add_ele incremental pulses into total kWh."""
        if raw is None or not isinstance(raw, (int, float)):
            if self._restored:
                self._state = round(self._total, DEFAULT_PRECISION)
            return

        scaled = self._scaled_value(raw, scale_factor)
        if scaled is None:
            return

        if scaled > 0 and self._should_accumulate_pulse(raw):
            self._total += scaled
            if self._event_time is not None:
                self._last_event_time = self._event_time
            self.debug(
                "add_ele pulse for %s: +%s (raw=%s, t=%s) total=%s",
                self.name,
                scaled,
                raw,
                self._event_time,
                self._total,
            )

        self._last_raw = raw
        self._total = float(self._total)
        self._state = round(self._total, DEFAULT_PRECISION)
        self._restored = True
        if not self._device.is_connecting:
            self._last_state = self._state

    def async_set_energy(self, value):
        """Set accumulated energy total to a specific kWh value."""
        if not self._is_energy_mode:
            return False
        try:
            total = float(value)
        except (TypeError, ValueError):
            return False
        if total < 0:
            return False
        self._total = total
        self._last_raw = None
        self._last_event_time = None
        self._state = round(self._total, DEFAULT_PRECISION)
        self._last_state = self._state
        self._restored = True
        self.schedule_update_ha_state()
        self.debug("Energy total set for %s: %s", self.name, self._state)
        return True

    def async_reset_energy(self):
        """Reset accumulated energy total to zero."""
        return self.async_set_energy(0)

    # No need to push restored value back to the device for a sensor
    async def restore_state_when_connected(self):
        """Do nothing for a sensor."""
        return


async_setup_entry = partial(async_setup_entry, DOMAIN, LocaltuyaSensor, flow_schema)
