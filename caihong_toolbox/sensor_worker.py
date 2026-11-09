import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from caihong_toolbox.sensors import PROXY_NAME, PROXY_PATH, PROXY_SENSORS, inventory, read_sensor


def emit(event, **values):
    print(json.dumps({'event': event, 'time': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
                      **values}, ensure_ascii=False), flush=True)


def main():
    from gi.repository import Gio, GLib
    parser = argparse.ArgumentParser(description='普通用户单传感器只读观察')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--list', action='store_true')
    group.add_argument('--sensor')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--parent', type=int)
    arguments = parser.parse_args()
    if arguments.parent is not None and os.getppid() != arguments.parent:
        raise RuntimeError('观察窗口已退出，未启动传感器')
    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)

    def call(path, interface, method, parameters=None):
        return bus.call_sync(PROXY_NAME, path, interface, method, parameters, None,
                              Gio.DBusCallFlags.NO_AUTO_START, 1000, None).unpack()

    def properties(path, interface):
        return call(path, 'org.freedesktop.DBus.Properties', 'GetAll', GLib.Variant('(s)', (interface,)))[0]

    available, warnings = {}, []
    for interface, path in ((PROXY_NAME, PROXY_PATH), (PROXY_NAME + '.Compass', PROXY_PATH + '/Compass')):
        try:
            available[interface] = properties(path, interface)
        except GLib.Error as error:
            warnings.append(error.message)
    devices = inventory(proxy=available)
    if arguments.list:
        emit('inventory', sensors=devices, warnings=warnings)
        return
    selected = next((sensor for sensor in devices if sensor['id'] == arguments.sensor), None)
    if selected is None:
        raise ValueError('选中的传感器已不可用，请刷新列表；不接受任意文件路径')
    loop = GLib.MainLoop()
    cancelled = False

    def stop(*parameters):
        nonlocal cancelled
        cancelled = True
        loop.quit()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def wait_for_updates():
        if arguments.once or cancelled:
            return
        def parent_alive():
            if arguments.parent is not None and os.getppid() != arguments.parent:
                stop()
            return True
        watcher = GLib.timeout_add(500, parent_alive)
        try:
            loop.run()
        finally:
            GLib.source_remove(watcher)

    if selected['source'] != 'proxy':
        def poll():
            emit('sample', **read_sensor(selected))
            return not cancelled
        emit('ready', sensor=selected)
        poll()
        if not arguments.once and not cancelled:
            timer = GLib.timeout_add(250, poll)
            try:
                wait_for_updates()
            finally:
                GLib.source_remove(timer)
        return
    definition = PROXY_SENSORS[selected['id'].removeprefix('proxy:')]
    claimed, subscription = False, None
    try:
        def changed(connection, sender, path, interface, member, parameters, user_data):
            changed_interface, values, invalidated = parameters.unpack()
            if changed_interface != definition['interface']:
                return
            if values.get(definition['has']) is False:
                emit('error', message='该传感器已离线')
                stop()
                return
            if any(name in values for name in definition['values']):
                emit('sample', id=selected['id'], values={name: values[name] for name in definition['values'] if name in values},
                     errors={}, origin='update')
        subscription = bus.signal_subscribe(PROXY_NAME, 'org.freedesktop.DBus.Properties', 'PropertiesChanged',
                                              definition['path'], definition['interface'], Gio.DBusSignalFlags.NONE,
                                              changed, None)
        call(definition['path'], definition['interface'], definition['claim'])
        claimed = True
        values = properties(definition['path'], definition['interface'])
        emit('ready', sensor=selected)
        emit('sample', id=selected['id'], values={name: values[name] for name in definition['values'] if name in values},
             errors={}, origin='initial')
        if not arguments.once and not cancelled:
            wait_for_updates()
    finally:
        if subscription is not None:
            bus.signal_unsubscribe(subscription)
        if claimed:
            call(definition['path'], definition['interface'], definition['release'])


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        emit('error', message=str(error))
        raise SystemExit(1)
