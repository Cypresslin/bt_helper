# Copyright 2016 Canonical Ltd.
# Written by:
#   Maciej Kisielewski <maciej.kisielewski@canonical.com>
#
# This is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3,
# as published by the Free Software Foundation.
#
# This file is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this file.  If not, see <http://www.gnu.org/licenses/>.
"""
This module provides a set of abstractions to ease the process of automating
typical Bluetooth task like scanning for devices and pairing with them.

It talks with BlueZ stack using dbus.
"""
import dbus
import dbus.mainloop.glib
import sys
import logging
import time
from gi.repository import GObject

logger = logging.getLogger(__file__)
logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.DEBUG)

IFACE = 'org.bluez.Adapter1'
ADAPTER_IFACE = 'org.bluez.Adapter1'
DEVICE_IFACE = 'org.bluez.Device1'

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

BT_ANY = 0
BT_KEYBOARD = int('0x2540', 16)

class BtDbusManager:
    """ Main point of contact with dbus factoring bt objects. """
    def __init__(self):
        self._bus = dbus.SystemBus()
        self._bt_root = self._bus.get_object('org.bluez', '/')
        self._manager = dbus.Interface(
            self._bt_root, 'org.freedesktop.DBus.ObjectManager')
        self._main_loop = GObject.MainLoop()

    def _get_objects_by_iface(self, iface_name):
        for path, ifaces in self._manager.GetManagedObjects().items():
            if ifaces.get(iface_name):
                yield self._bus.get_object('org.bluez', path)

    def get_bt_adapters(self):
        """Yield BtAdapter objects for each BT adapter found."""
        for adapter in self._get_objects_by_iface(ADAPTER_IFACE):
            yield BtAdapter(dbus.Interface(adapter, ADAPTER_IFACE), self)

    def get_bt_devices(self, category=BT_ANY, filters={}):
        """Yields BtDevice objects currently known to the system.

        filters - specifies the characteristics of that a BT device must have
        to be yielded. The keys of filters dictionary represent names of
        parameters (as specified by the bluetooth DBus Api and represented by
        DBus proxy object), and its values must match proxy values.
        I.e. {'Paired': False}. For a full list of Parameters see:
        http://git.kernel.org/cgit/bluetooth/bluez.git/tree/doc/device-api.txt

        Note that this function returns objects corresponding to BT devices
        that were seen last time scanning was done."""
        for device in self._get_objects_by_iface(DEVICE_IFACE):
            obj = self.get_object_by_path(device.object_path)[DEVICE_IFACE]
            try:
                if category != BT_ANY:
                    if obj['Class'] != category:
                        continue
                rejected = False
                for filter in filters:
                    if obj[filter] != filters[filter]:
                        rejected = True
                        break
                if rejected:
                    continue
                yield BtDevice(dbus.Interface(device, DEVICE_IFACE), self)
            except KeyError as exc:
                logger.info('Property %s not found on device %s',
                            exc, device.object_path)
                continue

    def get_prop_iface(self, obj):
        return dbus.Interface(self._bus.get_object(
            'org.bluez', obj.object_path), 'org.freedesktop.DBus.Properties')

    def get_object_by_path(self, path):
        return self._manager.GetManagedObjects()[path]

    def wait(self):
        self._main_loop.run()

    def resume(self):
        self._main_loop.quit()

    def scan(self, timeout=10):
        """Scan for BT devices visible to all adapters.'"""
        self._bus.add_signal_receiver(interfaces_added,
                dbus_interface = "org.freedesktop.DBus.ObjectManager",
                signal_name = "InterfacesAdded")
        self._bus.add_signal_receiver(properties_changed,
                dbus_interface = "org.freedesktop.DBus.Properties",
                signal_name = "PropertiesChanged",
                arg0 = "org.bluez.Device1",
                path_keyword = "path")
        for adapter in self._get_objects_by_iface(ADAPTER_IFACE):
            try:
                dbus.Interface(adapter, ADAPTER_IFACE).StopDiscovery()
            except dbus.exceptions.DBusException:
                pass
            dbus.Interface(adapter, ADAPTER_IFACE).StartDiscovery()
        GObject.timeout_add_seconds(timeout, self._scan_timeout)
        self._main_loop.run()

    def _scan_timeout(self):
        for adapter in self._get_objects_by_iface(ADAPTER_IFACE):
            dbus.Interface(adapter, ADAPTER_IFACE).StopDiscovery()
        self._main_loop.quit()

class BtAdapter:
    def __init__(self, dbus_iface, bt_mgr):
        self._if = dbus_iface
        self._bt_mgr = bt_mgr
        self._prop_if = bt_mgr.get_prop_iface(dbus_iface)

    def set_bool_prop(self, prop_name, value):
        self._prop_if.Set(IFACE, prop_name, dbus.Boolean(value))

    def ensure_powered(self):
        """Turn the adapter on, and do nothing if already on."""
        powered = self._prop_if.Get(IFACE, 'Powered')
        logger.info('Powering on {}'.format(self._if.object_path.split('/')[-1]))
        if powered:
            logger.info('Device already powered')
            return
        try:
            self.set_bool_prop('Powered', True)
            logger.info('Powered on')
        except Exception as exc:
            logging.error('Failed to power on - {}'.format(exc.get_dbus_message()))

class BtDevice:
    def __init__(self, dbus_iface, bt_mgr):
        self._if = dbus_iface
        self._bt_mgr = bt_mgr

    def pair(self):
        """Pair the device.

        This function will try pairing with the device and block until device
        is paired, error occured or default timeout elapsed (whichever comes
        first).
        """
        self._if.Pair(
            reply_handler=self._pair_ok, error_handler=self._pair_error)
        self._bt_mgr.wait()
        try:
            self._if.Connect()
        except dbus.exceptions.DBusException as exc:
            logging.error('Failed to connect - {}'.format(exc.get_dbus_message()))
    
    
    def unpair(self):
        self._if.Disconnect()
        # We will need to remove the device here
        # this can be done by calling dbus.Interface(self, ADAPTER_IFACE).RemoveDevice(device_obj)


    def _pair_ok(self):
        logger.info('%s successfully paired', self._if.object_name)
        self._bt_mgr.resume()

    def _pair_error(self, error):
        logger.warning('Pairing of %s device failed. %s', self._if.object_name,
                    error)
        self._bt_mgr.resume()


def properties_changed(interface, changed, invalidated, path):
    logger.info('Property changed for device @ %s. Change: %s', path, changed)
        
        
def interfaces_added(path, interfaces):
    logger.info('Added new bt interfaces: %s @ %s', interfaces, path)
