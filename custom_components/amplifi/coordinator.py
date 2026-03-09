"""The Amplifi coordinator."""
import logging
import aiohttp

from aiohttp.client_exceptions import ClientConnectorError
from datetime import timedelta

from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN
from .client import AmplifiClient, AmplifiClientError

_LOGGER = logging.getLogger(__name__)

WIFI_DEVICES_IDX = 1
DEVICES_INFO_IDX = 2
ETHERNET_PORT_TO_DEVICE_IDX = 3
ETHERNET_PORTS_IDX = 4


class AmplifiDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Amplifi data from router."""

    def __init__(self, hass, hostname, password):
        """Initialize."""
        self._hostname = hostname
        self._password = password
        self._wifi_devices = {}
        self._devices_info = {}
        self._ethernet_ports = {}
        self._ethernet_devices = {}
        self._wan_speeds = {"download": 0, "upload": 0}
        self._router_mac_addr = None
        # Create jar for storing session cookies
        self._jar = aiohttp.CookieJar(unsafe=True)
        # Amplifi uses session cookie so we need a web client with a cookie jar
        self._client_sesssion = async_create_clientsession(
            hass, False, True, cookie_jar=self._jar
        )
        self._client = AmplifiClient(
            self._client_sesssion, self._hostname, self._password
        )

        # TODO: Make this a configurable value
        update_interval = timedelta(seconds=10)
        _LOGGER.debug("Data will be update every %s", update_interval)

        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=update_interval)

        # FIX: Use self.async_add_listener (not super().async_add_listener).
        # Calling super().async_add_listener bypasses the coordinator's internal
        # listener tracking, which causes "Task exception was never retrieved"
        # because the callbacks are scheduled as bare tasks without error handling.
        self.async_add_listener(self.extract_wifi_devices)
        self.async_add_listener(self.extract_devices_info)
        self.async_add_listener(self.extract_ethernet_ports)
        self.async_add_listener(self.extract_ethernet_devices)
        self.async_add_listener(self.extract_wan_speeds)

    async def _async_update_data(self):
        """Update data via library."""
        try:
            # FIX: async_timeout.timeout is deprecated; use asyncio.timeout instead (HA 2024+)
            import asyncio
            async with asyncio.timeout(10):
                devices = await self._client.async_get_devices()
        except (AmplifiClientError, ClientConnectorError) as error:
            raise UpdateFailed(error) from error
        return devices

    @staticmethod
    def _looks_like_devices_info(candidate):
        """Return True when a dict resembles Amplifi devices_info payload."""
        if not isinstance(candidate, dict) or not candidate:
            return False
        sample_val = next(iter(candidate.values()))
        if not isinstance(sample_val, dict):
            return False
        expected = {"connection", "description", "host_name", "ip", "port"}
        return bool(expected.intersection(sample_val.keys()))

    @staticmethod
    def _data_index(data, index):
        """Safely return list index or None."""
        if isinstance(data, list) and len(data) > index:
            return data[index]
        return None

    def extract_wifi_devices(self):
        """Extract wifi devices from raw response after a successful update."""
        if self.data is None:
            self._wifi_devices = {}
            return

        wifi_devices = {}
        raw_wifi_devices = self._data_index(self.data, WIFI_DEVICES_IDX)
        if isinstance(raw_wifi_devices, dict):
            for access_point, bands in raw_wifi_devices.items():
                if not isinstance(bands, dict):
                    continue
                for _, network_types in bands.items():
                    if not isinstance(network_types, dict):
                        continue
                    for _, devices in network_types.items():
                        if not isinstance(devices, dict):
                            continue
                        for mac_addr, device_info in devices.items():
                            if not isinstance(device_info, dict):
                                continue
                            entry = dict(device_info)
                            entry["connected_to"] = access_point
                            wifi_devices[mac_addr] = entry

        self._wifi_devices = wifi_devices

    def extract_devices_info(self):
        """Extract known devices info from raw response."""
        if self.data is None:
            self._devices_info = {}
            return

        # Canonical location in legacy payloads.
        candidate = self._data_index(self.data, DEVICES_INFO_IDX)
        if self._looks_like_devices_info(candidate):
            self._devices_info = candidate
            return

        # Fallback: scan full list payload for the first matching dict.
        if isinstance(self.data, list):
            for item in self.data:
                if self._looks_like_devices_info(item):
                    self._devices_info = item
                    return

        # Some firmwares may respond with a top-level dict.
        if self._looks_like_devices_info(self.data):
            self._devices_info = self.data
            return

        self._devices_info = {}

    def extract_ethernet_ports(self):
        if self.data is None:
            self._ethernet_ports = {}
            return
        router_mac_addr = self.get_router_mac_addr()
        raw_ports = self._data_index(self.data, ETHERNET_PORTS_IDX)

        if (
            isinstance(raw_ports, dict)
            and router_mac_addr is not None
            and router_mac_addr in raw_ports
        ):
            self._ethernet_ports = raw_ports[router_mac_addr]
        else:
            self._ethernet_ports = {}
        _LOGGER.debug(f"ports={self.ethernet_ports}")

    def extract_ethernet_devices(self):
        """Try get additional device info for connected ethernet ports."""
        if self.data is None:
            self._ethernet_devices = {}
            return
        router_mac_addr = self.get_router_mac_addr()
        raw_port_map = self._data_index(self.data, ETHERNET_PORT_TO_DEVICE_IDX)
        raw_devices_info = self.devices_info or {}
        ethernet_devices = {}

        if (
            isinstance(raw_port_map, dict)
            and router_mac_addr is not None
            and router_mac_addr in raw_port_map
        ):
            raw_device_to_eth_index = raw_port_map[router_mac_addr]

            if raw_device_to_eth_index and raw_devices_info:
                for device in raw_device_to_eth_index:
                    if device not in raw_devices_info:
                        continue
                    device_info = dict(raw_devices_info[device])
                    port = raw_device_to_eth_index[device]
                    device_info["connected_to_port"] = port
                    ethernet_devices[device] = device_info

            self._ethernet_devices = ethernet_devices

            _LOGGER.debug(f"ethernet_devices={self._ethernet_devices}")
        else:
            self._ethernet_devices = {}
            _LOGGER.debug("No ethernet devices found")
            return

    def extract_wan_speeds(self):
        if self.data is None:
            self._wan_speeds = {"download": 0, "upload": 0}
            return
        router_mac_addr = self.get_router_mac_addr()
        raw_ports = self._data_index(self.data, ETHERNET_PORTS_IDX)
        if (
            not isinstance(raw_ports, dict)
            or router_mac_addr is None
            or router_mac_addr not in raw_ports
            or "eth-0" not in raw_ports[router_mac_addr]
        ):
            self._wan_speeds = {"download": 0, "upload": 0}
            return
        wan_port_data = raw_ports[router_mac_addr]["eth-0"]
        if "rx_bitrate" in wan_port_data:
            self._wan_speeds["download"] = (
                0 if wan_port_data["rx_bitrate"] == 0
                else wan_port_data["rx_bitrate"] / 1024
            )
        if "tx_bitrate" in wan_port_data and wan_port_data["tx_bitrate"] != 0:
            self._wan_speeds["upload"] = (
                0 if wan_port_data["tx_bitrate"] == 0
                else wan_port_data["tx_bitrate"] / 1024
            )

        _LOGGER.debug(f"wan_speeds={self._wan_speeds}")

    def find_router_mac_in_topology(self, topology_data):
        if isinstance(topology_data, dict):
            for k, v in topology_data.items():
                if k == "role" and v == "Router" and "mac" in topology_data:
                    self._router_mac_addr = topology_data["mac"]
                elif isinstance(v, dict):
                    self.find_router_mac_in_topology(v)
                elif isinstance(v, list):
                    for node in v:
                        self.find_router_mac_in_topology(node)
        elif isinstance(topology_data, list):
            for node in topology_data:
                self.find_router_mac_in_topology(node)

    def get_router_mac_addr(self):
        if self._router_mac_addr is None:
            topology = self._data_index(self.data, 0)
            if topology is not None:
                self.find_router_mac_in_topology(topology)

        return self._router_mac_addr

    # FIX: Removed broken async_stop_refresh() — super._async_stop_refresh() is not callable
    # (missing parentheses on super, and the method doesn't exist in DataUpdateCoordinator).
    # HA handles coordinator shutdown automatically via config entry unload.

    @property
    def wifi_devices(self):
        """Return the wifi devices."""
        return self._wifi_devices

    @property
    def devices_info(self):
        """Return known devices info."""
        return self._devices_info

    @property
    def ethernet_ports(self):
        """Return the ethernet ports."""
        return self._ethernet_ports

    @property
    def ethernet_devices(self):
        """Return the ethernet devices."""
        return self._ethernet_devices

    @property
    def wan_speeds(self):
        """Return the wan speeds."""
        return self._wan_speeds
