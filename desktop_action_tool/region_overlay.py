"""Owned, timed region frames; reuse the activity frame's focus-free IPC lifecycle."""
import ctypes
from ctypes import wintypes as w
import json
import math
import os
import subprocess
import sys
import time
import uuid

from . import activity_indicator as indicator
from .action_runtime import ActionLock
from .image_capture import wait
from .image_geometry import error, intersection
from .image_store import no_links
from .project_paths import PROJECT_ROOT
from .win32_api import BITMAPINFO, BITMAPINFOHEADER
from .worker_client import WorkerJob


class RegionWorker(indicator.FrameWorker):
    state_name = '.region_overlay.json'

    def monitors(self):
        return [tuple(self.payload['rectangle'])]

    def region(self, handle, width, height):
        # Alpha-zero interior and WS_DISABLED/TRANSPARENT suffice for hit testing.
        pass

    def render(self, handle, x, y, width, height):
        dc = self.api.gdi.CreateCompatibleDC(None)
        bitmap = previous = None
        if not dc:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            info = BITMAPINFO()
            info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            info.bmiHeader.biWidth, info.bmiHeader.biHeight = width, -height
            info.bmiHeader.biPlanes, info.bmiHeader.biBitCount = 1, 32
            address = ctypes.c_void_p()
            bitmap = self.api.gdi.CreateDIBSection(dc, ctypes.byref(info), 0, ctypes.byref(address), None, 0)
            if not bitmap or not address.value:
                raise ctypes.WinError(ctypes.get_last_error())
            previous = self.api.gdi.SelectObject(dc, bitmap)
            # Premultiplied blue, 70% opacity. Tiny selections remain visible.
            color = bytes((179, 119, 0, 179))
            border = min(3, max(1, min(width, height) // 2))
            row = color * width
            middle = color * border + bytes(max(0, width - 2 * border) * 4) + color * min(border, max(0, width - border))
            pixels = b''.join(row if iy < border or iy >= height - border else middle for iy in range(height))
            ctypes.memmove(address, pixels, len(pixels))
            destination, origin, size = w.POINT(x, y), w.POINT(), w.SIZE(width, height)
            blend = indicator.BLENDFUNCTION(0, 0, 255, 1)
            if not self.api.user.UpdateLayeredWindow(handle, None, ctypes.byref(destination), ctypes.byref(size),
                    dc, ctypes.byref(origin), 0, ctypes.byref(blend), 2):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            if previous:
                self.api.gdi.SelectObject(dc, previous)
            if bitmap:
                self.api.gdi.DeleteObject(bitmap)
            self.api.gdi.DeleteDC(dc)


def live_rectangle(store, meta, rect):
    mappings = meta['mappings']
    if len(mappings) != 1 or meta['kind'] == 'composite':
        error('IMAGE_OVERLAY_UNAVAILABLE', 'screen frame requires one unambiguous capture source')
    mapping = mappings[0]
    if intersection(mapping['content_rect'], rect) != rect:
        error('IMAGE_OVERLAY_UNAVAILABLE', 'selection includes padding or labels without screen coordinates')
    capture = mapping['capture']
    age = store.clock() - capture['captured_epoch']
    if age < 0 or age > store.options['image_overlay_freshness_s']:
        error('IMAGE_SOURCE_STALE', 'screen frame source is too old; frozen preview remains usable')
    x, y, width, height = rect
    sx, sy = mapping['scale']
    ox, oy = mapping['offset']
    screen_x, screen_y = capture['screen_rect'][:2]
    return [math.floor(screen_x + (x - ox) / sx), math.floor(screen_y + (y - oy) / sy),
            math.ceil(screen_x + (x + width - ox) / sx), math.ceil(screen_y + (y + height - oy) / sy)], capture


def show(store, meta, rect, duration_ms):
    from . import window_backend as windows
    state = indicator.bound_session(store.root)
    if state and state.get('profile') == 'background':
        error('PROFILE_VIOLATION', 'background profile forbids a screen region frame')
    rectangle, capture = live_rectangle(store, meta, rect)
    if duration_ms == 0:
        return {'shown': False, 'duration_ms': 0}
    def check():
        store.check()
        if windows.verification_window_context(capture['window']['window_id']) != capture['window'] or windows.monitor_layout() != capture['monitors']:
            error('IMAGE_SOURCE_CHANGED', 'live window geometry, DPI or monitors changed; frame closed')
    with ActionLock(store.root / '.action_state.json'):
        windows.initialize_dpi_awareness()
        check()
        store.folder.mkdir(parents=True, exist_ok=True)
        path = store.folder / RegionWorker.state_name
        no_links(path, store.root)
        no_links(path.with_suffix('.tmp'), store.root)
        identifier = uuid.uuid4().hex
        payload = {'path': str(path), 'session_id': identifier, 'rectangle': rectangle, 'persistent': False,
            'timeout_s': 40, 'width': 3, 'gradient': 0, 'opacity': 70, 'owner_pid': os.getpid(), 'abort_on_escape': False}
        process = subprocess.Popen([sys.executable, '-B', '-m', 'desktop_action_tool.region_overlay', '--worker'],
            cwd=PROJECT_ROOT, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        job = client = None
        try:
            job = WorkerJob(process)
            process.stdin.write(json.dumps(payload).encode('utf-8'))
            process.stdin.close()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and process.poll() is None:
                check()
                client = indicator.find_client(path)
                if client and client.state['session_id'] == identifier:
                    break
                if client:
                    client.close()
                    client = None
                time.sleep(.02)
            if client is None:
                error('IMAGE_OVERLAY_UNAVAILABLE', 'region frame worker did not become ready')
            wait(check, duration_ms / 1000)
            return {'shown': True, 'duration_ms': duration_ms, 'screen_rect': rectangle, 'closed': True}
        finally:
            try:
                if client:
                    try:
                        client.request(indicator.STOP)
                    finally:
                        client.close()
            finally:
                if job:
                    job.close()
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                # Delete only this worker's descriptor, including after hard exits.
                state = indicator.read_state(path)
                if state and state.get('session_id') == identifier:
                    path.unlink(missing_ok=True)


if __name__ == '__main__':
    if sys.argv[1:] != ['--worker']:
        raise SystemExit('Use preview_region(show_overlay=true)')
    from .window_backend import initialize_dpi_awareness
    initialize_dpi_awareness()
    RegionWorker(json.load(sys.stdin)).run()
