from __future__ import annotations

import abc
import asyncio
import logging
import os
import random
from typing import Any

import zigpy.appdb
import zigpy.config as conf
import zigpy.device
import zigpy.exceptions
import zigpy.group
import zigpy.ota
import zigpy.quirks
import zigpy.state
import zigpy.topology
import zigpy.types as t
import zigpy.util
import zigpy.zcl
import zigpy.zdo
import zigpy.zdo.types as zdo_types

DEFAULT_ENDPOINT_ID = 1
LOGGER = logging.getLogger(__name__)


class ControllerApplication(zigpy.util.ListenableMixin, abc.ABC):
    SCHEMA = conf.CONFIG_SCHEMA
    SCHEMA_DEVICE = conf.SCHEMA_DEVICE

    def __init__(self, config: dict):
        self.devices: dict[t.EUI64, zigpy.device.Device] = {}
        self.state: zigpy.state.State = zigpy.state.State()
        self.topology = None
        self._listeners = {}
        self._config = self.SCHEMA(config)
        self._dblistener = None
        self._groups = zigpy.group.Groups(self)
        self._listeners = {}
        self._ota = zigpy.ota.OTA(self)
        self._send_sequence = 0

    async def _load_db(self) -> None:
        """Restore save state."""
        database_file = self.config[conf.CONF_DATABASE]
        if not database_file:
            return

        self._dblistener = await zigpy.appdb.PersistingListener.new(database_file, self)
        self.add_listener(self._dblistener)
        self.groups.add_listener(self._dblistener)
        await self._dblistener.load()

    async def startup(self, *, auto_form: bool = False):
        """
        Starts a network, optionally forming one with random settings if necessary.
        """

        await self.connect()

        try:
            try:
                await self.load_network_info(load_devices=False)
            except zigpy.exceptions.NetworkNotFormed:
                LOGGER.info("Network is not formed")

                if not auto_form:
                    raise

                await self.form_network()

            await self.start_network()

            # Some radios erroneously permit joins on startup
            await self.permit(0)
        except Exception:
            LOGGER.error("Couldn't start application")
            await self.shutdown()
            raise

    @classmethod
    async def new(
        cls, config: dict, auto_form: bool = False, start_radio: bool = True
    ) -> ControllerApplication:
        """Create new instance of application controller."""
        app = cls(config)
        await app._load_db()
        await app.ota.initialize()
        app.topology = zigpy.topology.Topology.new(app)

        if not start_radio:
            return app

        await app.startup(auto_form=auto_form)

        for device in app.devices.values():
            if not device.is_initialized:
                LOGGER.warning("Device is partially initialized: %s", device)

        return app

    async def form_network(self):
        """
        Writes random network settings to the coordinator.
        """

        # First, make the settings consistent and randomly generate missing values
        channel = self.config[conf.CONF_NWK][conf.CONF_NWK_CHANNEL]
        channels = self.config[conf.CONF_NWK][conf.CONF_NWK_CHANNELS]
        pan_id = self.config[conf.CONF_NWK][conf.CONF_NWK_PAN_ID]
        extended_pan_id = self.config[conf.CONF_NWK][conf.CONF_NWK_EXTENDED_PAN_ID]
        network_key = self.config[conf.CONF_NWK][conf.CONF_NWK_KEY]

        if pan_id is None:
            pan_id = random.SystemRandom().randint(0x0001, 0xFFFE + 1)

        if extended_pan_id is None:
            # TODO: exclude `FF:FF:FF:FF:FF:FF:FF:FF` and possibly more reserved EPIDs
            extended_pan_id = t.ExtendedPanId(os.urandom(8))

        if network_key is None:
            network_key = t.KeyData(os.urandom(16))

        # Override `channels` with a single channel if one is explicitly set
        if channel is not None:
            channels = t.Channels.from_channel_list([channel])

        nwk = 0x0000
        ieee = None

        network_info = zigpy.state.NetworkInfo(
            extended_pan_id=extended_pan_id,
            pan_id=pan_id,
            nwk_update_id=self.config[conf.CONF_NWK][conf.CONF_NWK_UPDATE_ID],
            nwk_manager_id=0x0000,
            channel=channel,
            channel_mask=channels,
            security_level=5,
            network_key=zigpy.state.Key(
                key=network_key,
                tx_counter=0,
                rx_counter=0,
                seq=self.config[conf.CONF_NWK][conf.CONF_NWK_KEY_SEQ],
                partner_ieee=ieee,
            ),
            tc_link_key=zigpy.state.Key(
                key=self.config[conf.CONF_NWK][conf.CONF_NWK_KEY],
                tx_counter=0,
                rx_counter=0,
                seq=0,
                partner_ieee=self.config[conf.CONF_NWK][conf.CONF_NWK_TC_ADDRESS],
            ),
            children=[],
            key_table=[],
            nwk_addresses={},
            stack_specific={},
        )

        node_info = zigpy.state.NodeInfo(
            nwk=nwk,
            ieee=ieee,
            logical_type=zdo_types.LogicalType.Coordinator,
        )

        await self.write_network_info(network_info=network_info, node_info=node_info)

    async def shutdown(self) -> None:
        """Shutdown controller."""
        if self._dblistener:
            await self._dblistener.shutdown()
        await self.disconnect()

    def add_device(self, ieee: t.EUI64, nwk: t.NWK):
        """
        Creates a zigpy `Device` object with the provided IEEE and NWK addresses.
        """

        assert isinstance(ieee, t.EUI64)
        # TODO: Shut down existing device

        dev = zigpy.device.Device(self, ieee, nwk)
        self.devices[ieee] = dev
        return dev

    def device_initialized(self, device):
        """Used by a device to signal that it is initialized"""
        LOGGER.debug("Device is initialized %s", device)

        self.listener_event("raw_device_initialized", device)
        device = zigpy.quirks.get_device(device)
        self.devices[device.ieee] = device
        if self._dblistener is not None:
            device.add_context_listener(self._dblistener)
            device.neighbors.add_context_listener(self._dblistener)
        self.listener_event("device_initialized", device)

    async def remove(
        self, ieee: t.EUI64, remove_children: bool = True, rejoin: bool = False
    ) -> None:
        """Try to remove a device from the network.

        :param ieee: address of the device to be removed
        """
        assert isinstance(ieee, t.EUI64)
        dev = self.devices.get(ieee)
        if not dev:
            LOGGER.debug("Device not found for removal: %s", ieee)
            return

        dev.cancel_initialization()

        LOGGER.info("Removing device 0x%04x (%s)", dev.nwk, ieee)
        asyncio.create_task(
            self._remove_device(dev, remove_children=remove_children, rejoin=rejoin)
        )
        if dev.node_desc is not None and dev.node_desc.is_end_device:
            parents = [
                parent
                for parent in self.devices.values()
                for nei in parent.neighbors
                if nei.device is dev
            ]
            for parent in parents:
                LOGGER.debug(
                    "Sending leave request for %s to %s parent", dev.ieee, parent.ieee
                )
                opts = parent.zdo.LeaveOptions.RemoveChildren
                if rejoin:
                    opts |= parent.zdo.LeaveOptions.Rejoin
                parent.zdo.create_catching_task(
                    parent.zdo.Mgmt_Leave_req(dev.ieee, opts)
                )

        self.listener_event("device_removed", dev)

    async def _remove_device(
        self,
        device: zigpy.device.Device,
        remove_children: bool = True,
        rejoin: bool = False,
    ) -> None:
        """Send a remove request then pop the device."""
        try:
            await asyncio.wait_for(
                device.zdo.leave(remove_children=remove_children, rejoin=rejoin),
                timeout=30
                if device.node_desc is not None and device.node_desc.is_end_device
                else 7,
            )
        except (zigpy.exceptions.DeliveryError, asyncio.TimeoutError) as ex:
            LOGGER.debug("Sending 'zdo_leave_req' failed: %s", ex)

        self.devices.pop(device.ieee, None)

    def deserialize(
        self,
        sender: zigpy.device.Device,
        endpoint_id: t.uint8_t,
        cluster_id: t.uint16_t,
        data: bytes,
    ) -> tuple[Any, bytes]:
        return sender.deserialize(endpoint_id, cluster_id, data)

    def handle_message(
        self,
        sender: zigpy.device.Device,
        profile: int,
        cluster: int,
        src_ep: int,
        dst_ep: int,
        message: bytes,
        *,
        dst_addressing: None
        | (t.Addressing.Group | t.Addressing.IEEE | t.Addressing.NWK) = None,
    ) -> None:
        """
        Called when the radio library receives a packet
        """
        self.listener_event(
            "handle_message", sender, profile, cluster, src_ep, dst_ep, message
        )

        if sender.is_initialized:
            return sender.handle_message(
                profile,
                cluster,
                src_ep,
                dst_ep,
                message,
                dst_addressing=dst_addressing,
            )

        LOGGER.debug(
            "Received frame on uninitialized device %s"
            " from ep %s to ep %s, cluster %s: %r",
            sender,
            src_ep,
            dst_ep,
            cluster,
            message,
        )

        if (
            dst_ep == 0
            or sender.all_endpoints_init
            or (
                sender.has_non_zdo_endpoints
                and cluster == zigpy.zcl.clusters.general.Basic.cluster_id
            )
        ):
            # Allow the following responses:
            #  - any ZDO
            #  - ZCL if endpoints are initialized
            #  - ZCL from Basic cluster if endpoints are initializing

            if not sender.initializing:
                sender.schedule_initialize()

            return sender.handle_message(
                profile,
                cluster,
                src_ep,
                dst_ep,
                message,
                dst_addressing=dst_addressing,
            )

        # Give quirks a chance to fast-initialize the device (at the moment only Xiaomi)
        zigpy.quirks.handle_message_from_uninitialized_sender(
            sender, profile, cluster, src_ep, dst_ep, message
        )

        # Reload the sender device object, in it was replaced by the quirk
        sender = self.get_device(ieee=sender.ieee)

        # If the quirk did not fast-initialize the device, start initialization
        if not sender.initializing and not sender.is_initialized:
            sender.schedule_initialize()

    def handle_join(self, nwk: t.NWK, ieee: t.EUI64, parent_nwk: t.NWK) -> None:
        """
        Called when a device joins or announces itself on the network.
        """

        ieee = t.EUI64(ieee)

        try:
            dev = self.get_device(ieee)
            LOGGER.info("Device 0x%04x (%s) joined the network", nwk, ieee)
            new_join = False
        except KeyError:
            dev = self.add_device(ieee, nwk)
            LOGGER.info("New device 0x%04x (%s) joined the network", nwk, ieee)
            new_join = True

        if dev.nwk != nwk:
            dev.nwk = nwk
            LOGGER.debug("Device %s changed id (0x%04x => 0x%04x)", ieee, dev.nwk, nwk)
            new_join = True

        if new_join:
            self.listener_event("device_joined", dev)
            dev.schedule_initialize()
        elif not dev.is_initialized:
            # Re-initialize partially-initialized devices but don't emit "device_joined"
            dev.schedule_initialize()
        else:
            # Rescan groups for devices that are not newly joining and initialized
            dev.schedule_group_membership_scan()

    def handle_leave(self, nwk: t.NWK, ieee: t.EUI64):
        """
        Called when a device has left the network.
        """
        LOGGER.info("Device 0x%04x (%s) left the network", nwk, ieee)

        try:
            dev = self.get_device(ieee)
        except KeyError:
            return
        else:
            self.listener_event("device_left", dev)

    @classmethod
    async def probe(cls, device_config: dict[str, Any]) -> bool | dict[str, Any]:
        """
        Probes the device specified by `device_config` and returns valid device settings
        if the radio supports the device. If the device is not supported, `False` is
        returned.
        """

        config = cls.SCHEMA({conf.CONF_DEVICE: cls.SCHEMA_DEVICE(device_config)})
        app = cls(config)

        try:
            await app.connect()
        except Exception:
            LOGGER.debug(
                "Failed to probe with config %s: %s",
                device_config,
                exc_info=True,
            )
            return False
        else:
            return device_config
        finally:
            await app.disconnect()

    @abc.abstractmethod
    async def connect(self):
        """
        Connect to the radio hardware and verify that it is compatible with the library.
        This method should be stateless if the connection attempt fails.
        """
        raise NotImplementedError()  # pragma: no cover

    @abc.abstractmethod
    async def disconnect(self):
        """
        Disconnects from the radio hardware and shuts down the network.
        """
        raise NotImplementedError()  # pragma: no cover

    @abc.abstractmethod
    async def start_network(self):
        """
        Starts a Zigbee network with settings currently stored in the radio hardware.
        """
        raise NotImplementedError()  # pragma: no cover

    @abc.abstractmethod
    async def force_remove(self, dev):
        """
        Instructs the radio to remove a device with a lower-level leave command. Not all
        radios implement this.
        """
        raise NotImplementedError()  # pragma: no cover

    async def mrequest(
        self,
        group_id: t.uint16_t,
        profile: t.uint8_t,
        cluster: t.uint16_t,
        src_ep: t.uint8_t,
        sequence: t.uint8_t,
        data: bytes,
        *,
        hops: int = 0,
        non_member_radius: int = 3,
    ):
        """Submit and send data out as a multicast transmission.

        :param group_id: destination multicast address
        :param profile: Zigbee Profile ID to use for outgoing message
        :param cluster: cluster id where the message is being sent
        :param src_ep: source endpoint id
        :param sequence: transaction sequence number of the message
        :param data: Zigbee message payload
        :param hops: the message will be delivered to all nodes within this number of
                     hops of the sender. A value of zero is converted to MAX_HOPS
        :param non_member_radius: the number of hops that the message will be forwarded
                                  by devices that are not members of the group. A value
                                  of 7 or greater is treated as infinite
        :returns: return a tuple of a status and an error_message. Original requestor
                  has more context to provide a more meaningful error message
        """
        raise NotImplementedError()  # pragma: no cover

    @abc.abstractmethod
    @zigpy.util.retryable_request
    async def request(
        self,
        device: zigpy.device.Device,
        profile: t.uint16_t,
        cluster: t.uint16_t,
        src_ep: t.uint8_t,
        dst_ep: t.uint8_t,
        sequence: t.uint8_t,
        data: bytes,
        expect_reply: bool = True,
        use_ieee: bool = False,
    ):
        """Submit and send data out as an unicast transmission.

        :param device: destination device
        :param profile: Zigbee Profile ID to use for outgoing message
        :param cluster: cluster id where the message is being sent
        :param src_ep: source endpoint id
        :param dst_ep: destination endpoint id
        :param sequence: transaction sequence number of the message
        :param data: Zigbee message payload
        :param expect_reply: True if this is essentially a request
        :param use_ieee: use EUI64 for destination addressing
        :returns: return a tuple of a status and an error_message. Original requestor
                  has more context to provide a more meaningful error message
        """
        raise NotImplementedError()  # pragma: no cover

    @abc.abstractmethod
    async def broadcast(
        self,
        profile: t.uint16_t,
        cluster: t.uint16_t,
        src_ep: t.uint8_t,
        dst_ep: t.uint8_t,
        grpid: t.uint16_t,
        radius: int,
        sequence: t.uint8_t,
        data: bytes,
        broadcast_address: t.BroadcastAddress,
    ):
        """Submit and send data out as an unicast transmission.

        :param profile: Zigbee Profile ID to use for outgoing message
        :param cluster: cluster id where the message is being sent
        :param src_ep: source endpoint id
        :param dst_ep: destination endpoint id
        :param: grpid: group id to address the broadcast to
        :param radius: max radius of the broadcast
        :param sequence: transaction sequence number of the message
        :param data: zigbee message payload
        :param timeout: how long to wait for transmission ACK
        :param broadcast_address: broadcast address.
        :returns: return a tuple of a status and an error_message. Original requestor
                  has more context to provide a more meaningful error message
        """
        raise NotImplementedError()  # pragma: no cover

    @abc.abstractmethod
    async def permit_ncp(self, time_s: int = 60):
        """
        Permit joining on NCP.
        Not all radios will require this method.
        """
        raise NotImplementedError()  # pragma: no cover

    @abc.abstractmethod
    async def permit_with_key(self, node: t.EUI64, code: bytes, time_s: int = 60):
        """
        Permit a node to join with the provided install code bytes.
        """
        raise NotImplementedError()  # pragma: no cover

    @abc.abstractmethod
    async def write_network_info(
        self,
        *,
        network_info: zigpy.state.NetworkInfo,
        node_info: zigpy.state.NodeInfo,
    ) -> None:
        """
        Writes network and node state to the radio hardware.
        Any information not supported by the radio should be logged as a warning.
        """
        raise NotImplementedError()  # pragma: no cover

    @abc.abstractmethod
    async def load_network_info(self, *, load_devices: bool = False) -> None:
        """
        Loads network and node information from the radio hardware.

        :param load_devices: if `False`, supplementary network information that may take
                             a while to load should be skipped. For example, device NWK
                             addresses and link keys.
        """
        raise NotImplementedError()  # pragma: no cover

    async def permit(self, time_s: int = 60, node: t.EUI64 | str | None = None):
        """Permit joining on a specific node or all router nodes."""
        assert 0 <= time_s <= 254
        if node is not None:
            if not isinstance(node, t.EUI64):
                node = t.EUI64([t.uint8_t(p) for p in node])
            if node != self.state.node_info.ieee:
                try:
                    dev = self.get_device(ieee=node)
                    r = await dev.zdo.permit(time_s)
                    LOGGER.debug("Sent 'mgmt_permit_joining_req' to %s: %s", node, r)
                except KeyError:
                    LOGGER.warning("Device '%s' not found", node)
                except zigpy.exceptions.DeliveryError as ex:
                    LOGGER.warning("Couldn't open '%s' for joining: %s", node, ex)
            else:
                await self.permit_ncp(time_s)
            return

        await zigpy.zdo.broadcast(
            self,  # app
            zdo_types.ZDOCmd.Mgmt_Permit_Joining_req,  # command
            0x0000,  # grpid
            0x00,  # radius
            time_s,
            0,
            broadcast_address=t.BroadcastAddress.ALL_ROUTERS_AND_COORDINATOR,
        )
        return await self.permit_ncp(time_s)

    def get_sequence(self) -> t.uint8_t:
        self._send_sequence = (self._send_sequence + 1) % 256
        return self._send_sequence

    def get_device(
        self, ieee: t.EUI64 = None, nwk: t.NWK | int = None
    ) -> zigpy.device.Device:
        """
        Looks up a device in the `devices` dictionary based either on its NWK or IEEE
        address.
        """

        if ieee is not None:
            return self.devices[ieee]

        # If there two coordinators are loaded from the database, we want the active one
        if nwk == self.state.node_info.nwk:
            return self.devices[self.state.node_info.ieee]

        # TODO: Make this not terrible
        # Unlike its IEEE address, a device's NWK address can change at runtime so this
        # is not as simple as building a second mapping
        for dev in self.devices.values():
            if dev.nwk == nwk:
                return dev

        raise KeyError("Device not found: nwk={nwk!r}, ieee={ieee!r}")

    def get_endpoint_id(self, cluster_id: int, is_server_cluster: bool = False) -> int:
        """Returns coordinator endpoint id for specified cluster id."""
        return DEFAULT_ENDPOINT_ID

    def get_dst_address(self, cluster) -> zdo_types.MultiAddress:
        """Helper to get a dst address for bind/unbind operations.

        Allows radios to provide correct information especially for radios which listen
        on specific endpoints only.
        :param cluster: cluster instance to be bound to coordinator
        :returns: returns a "destination address"
        """
        dstaddr = zdo_types.MultiAddress()
        dstaddr.addrmode = 3
        dstaddr.ieee = self.state.node_info.ieee
        dstaddr.endpoint = self.get_endpoint_id(cluster.cluster_id, cluster.is_server)
        return dstaddr

    def update_config(self, partial_config: dict[str, Any]) -> None:
        """Update existing config."""
        self.config = {**self.config, **partial_config}

    @property
    def config(self) -> dict:
        """Return current configuration."""
        return self._config

    @config.setter
    def config(self, new_config) -> None:
        """Configuration setter."""
        self._config = self.SCHEMA(new_config)

    @property
    def groups(self):
        return self._groups

    @property
    def ota(self):
        return self._ota
