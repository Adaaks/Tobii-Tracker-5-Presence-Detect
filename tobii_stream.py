"""Minimal ctypes binding to Tobii Stream Engine (ships with Tobii Experience).

Reads raw gaze points (normalized 0..1 on the display configured in Tobii
Experience) and head pose in a background thread.
"""
import ctypes as C
import os
import threading
import time
from collections import deque

DLL_CANDIDATES = [
    os.environ.get("TOBII_STREAM_ENGINE_DLL", ""),
    r"C:\Program Files\Tobii\Tobii EyeX\tobii_stream_engine.dll",
    r"C:\Program Files (x86)\Tobii\Tobii EyeX\tobii_stream_engine.dll",
    "tobii_stream_engine.dll",
]

VALID = 1
ERR_TIMED_OUT = 6
ERR_CONNECTION_FAILED = 5


class GazePoint(C.Structure):
    _fields_ = [("timestamp_us", C.c_int64),
                ("validity", C.c_int),
                ("position_xy", C.c_float * 2)]


class HeadPose(C.Structure):
    _fields_ = [("timestamp_us", C.c_int64),
                ("position_validity", C.c_int),
                ("position_xyz", C.c_float * 3),
                ("rotation_validity_xyz", C.c_int * 3),
                ("rotation_xyz", C.c_float * 3)]


URL_CB = C.CFUNCTYPE(None, C.c_char_p, C.c_void_p)
GAZE_CB = C.CFUNCTYPE(None, C.POINTER(GazePoint), C.c_void_p)
HEAD_CB = C.CFUNCTYPE(None, C.POINTER(HeadPose), C.c_void_p)
PRESENCE_CB = C.CFUNCTYPE(None, C.c_int, C.c_int64, C.c_void_p)

PRESENCE_AWAY, PRESENCE_PRESENT = 1, 2


def _load_dll():
    for p in DLL_CANDIDATES:
        if not p:
            continue
        try:
            return C.CDLL(p)
        except OSError:
            continue
    raise RuntimeError("tobii_stream_engine.dll not found. Install Tobii Experience, "
                       "or set TOBII_STREAM_ENGINE_DLL to its path.")


class TobiiStream:
    """Background reader. `latest()` / `since(t)` return (t_local, x, y, head, t_device_us)
    tuples, where head is (x, y, z, rx, ry, rz) in mm/radians or None."""

    def __init__(self, history_seconds=180.0):
        self.dll = _load_dll()
        self._setup_prototypes()
        self.samples = deque(maxlen=int(history_seconds * 200))
        self.head = None
        self.lock = threading.Lock()
        self.running = False
        self.has_head = False
        self.has_presence = False
        self.presence = 0      # 0 unknown, 1 away, 2 present (Tobii's own presence detection)
        self.head_t = 0.0      # local time of the last valid head position
        self.link_ok = True    # False while the tracker is unplugged / unreachable
        self.device_url = None
        self._api = C.c_void_p()
        self._dev = C.c_void_p()
        # keep callback refs alive
        self._gaze_cb = GAZE_CB(self._on_gaze)
        self._head_cb = HEAD_CB(self._on_head)
        self._presence_cb = PRESENCE_CB(self._on_presence)

    def _setup_prototypes(self):
        d = self.dll
        d.tobii_error_message.restype = C.c_char_p
        d.tobii_api_create.argtypes = [C.POINTER(C.c_void_p), C.c_void_p, C.c_void_p]
        d.tobii_enumerate_local_device_urls.argtypes = [C.c_void_p, URL_CB, C.c_void_p]
        d.tobii_device_create.argtypes = [C.c_void_p, C.c_char_p, C.c_int, C.POINTER(C.c_void_p)]
        d.tobii_gaze_point_subscribe.argtypes = [C.c_void_p, GAZE_CB, C.c_void_p]
        d.tobii_head_pose_subscribe.argtypes = [C.c_void_p, HEAD_CB, C.c_void_p]
        d.tobii_user_presence_subscribe.argtypes = [C.c_void_p, PRESENCE_CB, C.c_void_p]
        d.tobii_user_presence_unsubscribe.argtypes = [C.c_void_p]
        d.tobii_wait_for_callbacks.argtypes = [C.c_int, C.POINTER(C.c_void_p)]
        d.tobii_device_process_callbacks.argtypes = [C.c_void_p]
        d.tobii_device_reconnect.argtypes = [C.c_void_p]
        d.tobii_get_device_info.argtypes = [C.c_void_p, C.c_void_p]
        d.tobii_gaze_point_unsubscribe.argtypes = [C.c_void_p]
        d.tobii_head_pose_unsubscribe.argtypes = [C.c_void_p]
        d.tobii_device_destroy.argtypes = [C.c_void_p]
        d.tobii_api_destroy.argtypes = [C.c_void_p]

    def _check(self, err, what):
        if err != 0:
            msg = self.dll.tobii_error_message(err).decode(errors="replace")
            raise RuntimeError(f"{what} failed: {msg} ({err})")

    def _on_gaze(self, gp, _):
        g = gp.contents
        if g.validity != VALID:
            return
        with self.lock:
            self.samples.append((time.perf_counter(), g.position_xy[0], g.position_xy[1], self.head,
                                 g.timestamp_us))

    def _on_head(self, hp, _):
        h = hp.contents
        if h.position_validity == VALID:
            self.head_t = time.perf_counter()
        if h.position_validity != VALID or not all(v == VALID for v in h.rotation_validity_xyz):
            return
        self.head = tuple(h.position_xyz) + tuple(h.rotation_xyz)

    def _on_presence(self, status, _ts, _):
        self.presence = status

    def start(self):
        self._check(self.dll.tobii_api_create(C.byref(self._api), None, None), "api_create")
        try:
            return self._start_device()
        except Exception:
            if self._dev:
                self.dll.tobii_device_destroy(self._dev)
            self.dll.tobii_api_destroy(self._api)
            raise

    def _start_device(self):
        urls = []
        cb = URL_CB(lambda url, _: urls.append(url))
        self._check(self.dll.tobii_enumerate_local_device_urls(self._api, cb, None), "enumerate")
        if not urls:
            raise RuntimeError("No Tobii device found. Is it plugged in and is Tobii Experience running?")
        self.device_url = urls[0].decode()
        err = 1
        for field_of_use in (1, 2):  # interactive, then analytical
            err = self.dll.tobii_device_create(self._api, urls[0], field_of_use, C.byref(self._dev))
            if err == 0:
                break
        self._check(err, "device_create")
        self._check(self.dll.tobii_gaze_point_subscribe(self._dev, self._gaze_cb, None), "gaze subscribe")
        self.has_head = self.dll.tobii_head_pose_subscribe(self._dev, self._head_cb, None) == 0
        self.has_presence = self.dll.tobii_user_presence_subscribe(self._dev, self._presence_cb, None) == 0
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def _loop(self):
        devs = (C.c_void_p * 1)(self._dev)
        info = C.create_string_buffer(8192)  # tobii_device_info_t, generously sized
        next_check = 0.0
        while self.running:
            now = time.perf_counter()
            if now >= next_check:
                # Active health check every 2 s - works even when nobody is in front of the tracker
                # (data callbacks alone can't tell "unplugged" apart from "no one there").
                next_check = now + 2.0
                ok = self.dll.tobii_get_device_info(self._dev, info) == 0
                if not ok:
                    ok = self.dll.tobii_device_reconnect(self._dev) == 0
                self.link_ok = ok
                if not ok:
                    time.sleep(1.0)
                    continue
            err = self.dll.tobii_wait_for_callbacks(1, devs)
            if err in (0, ERR_TIMED_OUT):
                err = self.dll.tobii_device_process_callbacks(self._dev)
            if err not in (0, ERR_TIMED_OUT):
                self.link_ok = False
                next_check = 0.0  # check / reconnect right away
                time.sleep(0.5)

    def latest(self):
        with self.lock:
            return self.samples[-1] if self.samples else None

    def since(self, t0, t1=None):
        out = []
        with self.lock:
            for s in reversed(self.samples):  # newest first, stop once past t0
                if s[0] < t0:
                    break
                if t1 is None or s[0] <= t1:
                    out.append(s)
        out.reverse()
        return out

    def stop(self):
        if not self.running:
            return
        self.running = False
        self._thread.join(timeout=3.0)
        if self._thread.is_alive():
            return  # still stuck in a driver call (device gone) - don't free what it's using
        if self.has_head:
            self.dll.tobii_head_pose_unsubscribe(self._dev)
        if self.has_presence:
            self.dll.tobii_user_presence_unsubscribe(self._dev)
        self.dll.tobii_gaze_point_unsubscribe(self._dev)
        self.dll.tobii_device_destroy(self._dev)
        self.dll.tobii_api_destroy(self._api)


if __name__ == "__main__":
    s = TobiiStream().start()
    print("device:", s.device_url, "| head pose:", s.has_head)
    t0 = time.perf_counter()
    time.sleep(2)
    got = s.since(t0)
    print(f"{len(got)} samples in 2s (~{len(got)/2:.0f} Hz)")
    for x in got[-3:]:
        print(x)
    s.stop()
