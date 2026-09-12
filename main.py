import json
import time
import threading
import requests
import math
import ctypes
import locale
import os
from ctypes import cast, POINTER, pointer, sizeof, c_char_p, byref
from ctypes.wintypes import HANDLE, DWORD
from ctypes import c_long
import locale
# Fallback für Python-Versionen ohne HRESULT
try:
    from ctypes.wintypes import HRESULT
except ImportError:
    HRESULT = c_long


# --- MSFS / SimConnect ---
from SimConnect import *
from SimConnect.Enum import (
    SIMCONNECT_RECV_ID,
    SIMCONNECT_RECV_ASSIGNED_OBJECT_ID,
    SIMCONNECT_RECV_EXCEPTION,
    SIMCONNECT_DATA_INITPOSITION,
    SIMCONNECT_DATATYPE,
    SIMCONNECT_EXCEPTION,
)
from SimConnect.Constants import (
    SIMCONNECT_UNUSED,
    INITPOSITION_AIRSPEED_CRUISE,
)

from model_map import get_model_resolver
from routes import (
    RouteCache,
    is_on_ground,
    classify_airport_relation,
    airport_near,
    haversine_km as route_haversine_km,
    AIRPORT_RADIUS_KM,
)

# --- GUI ---
import tkinter as tk
from tkinter import messagebox
from tkintermapview import TkinterMapView

# High-DPI für scharfe GUI
ctypes.windll.shcore.SetProcessDpiAwareness(2)

API_USER_AGENT = "MyExtremeFlightTracker/1.0"

# =========================
# JSON COLOR THEME LOADER
# =========================
def load_color_scheme():
    default_scheme = {
        "map_settings": { "tile_server_url": "https://a.tile.openstreetmap.org/{z}/{x}/{y}.png" },
        "altitude_gradient": { "low": [0,0,255], "mid": [255,255,0], "high": [255,0,0] },
        "gui_theme": {
            "main_bg": "#f0f0f0", "panel_bg": "#e0e0e0", "text_color": "#000000",
            "entry_bg": "#ffffff", "entry_fg": "#000000", "btn_bg": "#d0d0d0", 
            "btn_fg": "#000000", "btn_disabled_bg": "#aaaaaa"
        }
    }
    try:
        json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "colors.json")
        if os.path.exists(json_path):
            with open(json_path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"[Theme] Fehler beim Laden der JSON, nutze Defaults: {e}")
    return default_scheme

COLOR_SCHEME = load_color_scheme()


# =========================
# MSFS CONNECTION + HELPERS
# =========================

APP_DIR = os.path.dirname(os.path.abspath(__file__))
BUNDLED_SIMCONNECT_DLL = os.path.join(APP_DIR, "SimConnect.dll")


def resolve_simconnect_dll():
    """Use only SimConnect.dll shipped next to this program."""
    if not os.path.isfile(BUNDLED_SIMCONNECT_DLL):
        print(f"[SimConnect] Missing bundled DLL: {BUNDLED_SIMCONNECT_DLL}")
        return None
    try:
        lib = ctypes.WinDLL(BUNDLED_SIMCONNECT_DLL)
        if not hasattr(lib, "SimConnect_AICreateNonATCAircraft_EX1"):
            print("[SimConnect] Bundled SimConnect.dll does not export EX1 spawn API")
            return None
    except OSError as e:
        print(f"[SimConnect] Cannot load bundled DLL: {e}")
        return None
    return BUNDLED_SIMCONNECT_DLL


def configure_simconnect_library():
    dll_path = resolve_simconnect_dll()
    if dll_path:
        import SimConnect.SimConnect as sc_module
        sc_module._library_path = dll_path
        print(f"[SimConnect] Using bundled DLL: {dll_path}")
        return dll_path
    print("[SimConnect] Place SimConnect.dll (MSFS 2024, with EX1) in the program folder.")
    return None


def _ensure_ex1_api(sm):
    """Bind MSFS 2024 EX1 spawn (required for modular / FSLTL aircraft)."""
    dll = sm.dll
    if getattr(dll, "_ex1_bound", False):
        return getattr(dll, "AICreateNonATCAircraft_EX1", None) is not None

    raw = dll.SimConnect
    if not hasattr(raw, "SimConnect_AICreateNonATCAircraft_EX1"):
        print("[SimConnect] EX1 export not found in loaded DLL")
        dll._ex1_bound = True
        return False

    fn = raw.SimConnect_AICreateNonATCAircraft_EX1
    fn.restype = HRESULT
    fn.argtypes = [
        HANDLE,
        c_char_p,
        c_char_p,
        c_char_p,
        POINTER(SIMCONNECT_DATA_INITPOSITION),
        DWORD,
    ]

    def _ex1_create(h, title, livery, tail, init_pos, request_id):
        return fn(h, title, livery, tail, byref(init_pos), request_id)

    dll.AICreateNonATCAircraft_EX1 = _ex1_create
    dll._ex1_bound = True
    print("[SimConnect] EX1 spawn API ready")
    return True


def _numeric_alt(alt):
    if alt is None or alt == "ground":
        return 0.0
    try:
        return float(alt)
    except (TypeError, ValueError):
        return 0.0


# MSFS only reliably spawns AI within ~35 km of the user aircraft.
MAX_SPAWN_DISTANCE_KM = float(os.environ.get("MSFS_SPAWN_RADIUS_KM", "35"))
MAX_ACTIVE_AI = int(os.environ.get("MSFS_MAX_AI_AIRCRAFT", "25"))
SPAWN_SETTLE_SEC = float(os.environ.get("MSFS_SPAWN_SETTLE_SEC", "0.35"))
PLAYER_AIRPORT_ICAO = os.environ.get("MSFS_AIRPORT_ICAO", "").strip().upper()
PARKED_DEPART_GS = float(os.environ.get("MSFS_PARKED_DEPART_GS", "25"))
MAX_PARKED_WAIT_SEC = float(os.environ.get("MSFS_MAX_PARKED_WAIT_SEC", "900"))

CREATE_OBJECT_FAILED = int(SIMCONNECT_EXCEPTION.SIMCONNECT_EXCEPTION_CREATE_OBJECT_FAILED)


def _haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _clamp_near_player(lat, lon, plat, plon, max_km=MAX_SPAWN_DISTANCE_KM):
    dist = _haversine_km(plat, plon, lat, lon)
    if dist <= max_km or dist <= 0:
        return lat, lon
    ratio = max_km / dist
    return plat + (lat - plat) * ratio, plon + (lon - plon) * ratio


def _sanitize_tail(callsign, registration=None):
    raw = (registration or callsign or "N12345").strip().upper()
    cleaned = "".join(c for c in raw if c.isalnum())
    return (cleaned or "N12345")[:12]


class AISpawnManager:
    """Sequential AI spawns with ASSIGNED_OBJECT_ID tracking and position updates."""

    # Request IDs >= 10000 avoid colliding with AircraftRequests / library IDs
    REQUEST_ID_BASE = 10000
    SPAWN_TIMEOUT_SEC = 20.0

    def __init__(self, sm):
        self.sm = sm
        if not _ensure_ex1_api(sm):
            raise RuntimeError(
                "SimConnect_AICreateNonATCAircraft_EX1 is not available. "
                "Use MSFS 2024 SDK SimConnect.dll (set MSFS_SIMCONNECT_DLL)."
            )
        self.pending_by_request = {}
        self.pending_by_send_id = {}
        self.spawn_queue = []
        self.spawning = False
        self.current_req_id = None
        self.spawn_started_at = 0.0
        self._next_request_id = self.REQUEST_ID_BASE
        self.pos_def_id = None
        self.player_lat = 0.0
        self.player_lon = 0.0
        self.player_alt = 3000.0
        self.player_airport_icao = PLAYER_AIRPORT_ICAO
        self._has_parked_api = hasattr(self.sm.dll.SimConnect, "SimConnect_AICreateParkedATCAircraft")
        self._patch_dispatch()

    def set_player_position(self, lat, lon, alt):
        if lat is not None and lon is not None:
            self.player_lat = float(lat)
            self.player_lon = float(lon)
        if alt is not None:
            self.player_alt = float(alt)

    def _alloc_request_id(self):
        rid = self._next_request_id
        self._next_request_id += 1
        return rid

    def _patch_dispatch(self):
        original = self.sm.my_dispatch_proc
        recv_assigned = int(SIMCONNECT_RECV_ID.SIMCONNECT_RECV_ID_ASSIGNED_OBJECT_ID)
        recv_exception = int(SIMCONNECT_RECV_ID.SIMCONNECT_RECV_ID_EXCEPTION)

        def patched(p_data):
            dw_id = int(p_data.contents.dwID)
            if dw_id == recv_assigned:
                evt = cast(p_data, POINTER(SIMCONNECT_RECV_ASSIGNED_OBJECT_ID)).contents
                self._on_object_id(int(evt.dwRequestID), int(evt.dwObjectID))
            elif dw_id == recv_exception:
                exc = cast(p_data, POINTER(SIMCONNECT_RECV_EXCEPTION)).contents
                self._on_exception(exc)
            return original(p_data)

        self.sm.my_dispatch_proc = patched

    def _on_object_id(self, request_id, object_id):
        job = self.pending_by_request.pop(request_id, None)
        if job is None and self.spawning and self.spawn_queue:
            job = self.spawn_queue[0]
            self.pending_by_request.pop(self.current_req_id, None)
        if job is None:
            return
        if job["dummy"].ai_id is not None:
            return
        job["dummy"].ai_id = int(object_id)
        job["dummy"].spawn_confirmed = True
        mode = job.get("spawn_mode", "airborne")
        print(f"[Spawn] {job['tail']} assigned object id {object_id} ({mode})")
        self._finish_current_spawn(job)

    def _on_exception(self, exc):
        exc_code = int(exc.dwException)
        req_id = self.pending_by_send_id.pop(int(exc.UNKNOWN_SENDID), None)
        if req_id is None:
            req_id = self.pending_by_send_id.pop(int(exc.dwSendID), None)
        if req_id is None and self.current_req_id is not None:
            req_id = self.current_req_id

        job = self.pending_by_request.pop(req_id, None) if req_id is not None else None
        if job is None and self.spawning and self.spawn_queue:
            job = self.spawn_queue[0]

        if job is None:
            return

        try:
            name = SIMCONNECT_EXCEPTION(exc_code).name
        except ValueError:
            name = f"exception_{exc_code}"

        if exc_code == CREATE_OBJECT_FAILED:
            print(f"[Spawn] {job['tail']}: {name} (title={job['title']!r})")
        self._fail_current_spawn(name, job)

    def _finish_current_spawn(self, job):
        if self.spawn_queue and self.spawn_queue[0] is job:
            self.spawn_queue.pop(0)
        self.spawning = False
        self.current_req_id = None
        if SPAWN_SETTLE_SEC > 0:
            time.sleep(SPAWN_SETTLE_SEC)
        self._try_spawn_next()

    def _fail_current_spawn(self, reason, job=None):
        if job is None and self.spawn_queue:
            job = self.spawn_queue[0]
        if self.current_req_id is not None:
            self.pending_by_request.pop(self.current_req_id, None)
        self.spawning = False
        self.current_req_id = None

        if job is not None:
            if job["dummy"].ai_id is not None:
                self._finish_current_spawn(job)
                return
            print(f"[Spawn] Failed for {job['tail']}: {reason}")
            if self.spawn_queue and self.spawn_queue[0] is job:
                self.spawn_queue.pop(0)

        self._try_spawn_next()

    def tick(self):
        """Unblock the spawn queue if the sim never returns an object id."""
        if not self.spawning or self.spawn_started_at <= 0:
            return

        job = self.spawn_queue[0] if self.spawn_queue else None
        if job and job["dummy"].ai_id is not None:
            self._finish_current_spawn(job)
            return

        env_id = os.environ.get("SIMCONNECT_OBJECT_ID")
        if job and env_id and env_id.isdigit():
            job["dummy"].ai_id = int(env_id)
            job["dummy"].spawn_confirmed = True
            print(f"[Spawn] {job['tail']} assigned object id {env_id} (from sim)")
            self._finish_current_spawn(job)
            return

        if (time.time() - self.spawn_started_at) > self.SPAWN_TIMEOUT_SEC:
            if job and job["dummy"].ai_id is not None:
                self._finish_current_spawn(job)
            else:
                self._fail_current_spawn("timeout waiting for object id")

    def queue_spawn(
        self,
        dummy,
        title,
        tail,
        lat,
        lon,
        alt,
        heading,
        ground_speed=0,
        spawn_mode="airborne",
        airport_icao="",
    ):
        self.spawn_queue.append({
            "dummy": dummy,
            "title": title,
            "tail": _sanitize_tail(tail),
            "lat": lat,
            "lon": lon,
            "alt": _numeric_alt(alt),
            "heading": float(heading or 0),
            "ground_speed": float(ground_speed or 0),
            "spawn_mode": spawn_mode,
            "airport_icao": (airport_icao or "").upper()[:8],
        })
        self._try_spawn_next()

    def _build_init_position(self, job):
        lat, lon = _clamp_near_player(
            job["lat"], job["lon"], self.player_lat, self.player_lon
        )
        alt = job["alt"]
        gs = float(job.get("ground_speed") or 0)
        if alt < 500 and gs > 40:
            alt = max(500.0, self.player_alt)
        elif alt < 50 and gs <= 40:
            alt = max(alt, 0.0)

        on_ground = 1 if gs < 30 and alt < 1000 else 0
        if on_ground:
            airspeed = 0
        else:
            airspeed = int(INITPOSITION_AIRSPEED_CRUISE.value)

        return SIMCONNECT_DATA_INITPOSITION(
            Latitude=lat,
            Longitude=lon,
            Altitude=alt,
            Pitch=0.0,
            Bank=0.0,
            Heading=job["heading"] % 360.0,
            OnGround=on_ground,
            Airspeed=airspeed,
        )

    def _try_spawn_next(self):
        if self.spawning or not self.spawn_queue:
            return

        job = self.spawn_queue[0]
        self.spawning = True

        init_pos = self._build_init_position(job)

        req_id = self._alloc_request_id()
        self.current_req_id = req_id
        self.spawn_started_at = time.time()
        self.pending_by_request[req_id] = job

        try:
            mode = job.get("spawn_mode", "airborne")
            if mode == "parked" and self._has_parked_api and job.get("airport_icao"):
                hr = self.sm.dll.AICreateParkedATCAircraft(
                    self.sm.hSimConnect,
                    job["title"].encode("utf-8"),
                    job["tail"].encode("utf-8"),
                    job["airport_icao"].encode("utf-8"),
                    req_id,
                )
                mode_label = f"parked@{job['airport_icao']}"
            else:
                hr = self.sm.dll.AICreateNonATCAircraft_EX1(
                    self.sm.hSimConnect,
                    job["title"].encode("utf-8"),
                    b"",
                    job["tail"].encode("utf-8"),
                    init_pos,
                    req_id,
                )
                mode_label = "airborne"

            send_id = DWORD(0)
            self.sm.dll.GetLastSentPacketID(self.sm.hSimConnect, byref(send_id))
            self.pending_by_send_id[int(send_id.value)] = req_id

            if self.sm.IsHR(hr, 0):
                print(
                    f"[Spawn] EX1 {mode_label} request sent for {job['tail']} "
                    f"(title={job['title']!r}) req={req_id}"
                )
            else:
                self.pending_by_request.pop(req_id, None)
                self.pending_by_send_id.pop(int(send_id.value), None)
                self._fail_current_spawn(f"HRESULT={hr}", job)
        except Exception as e:
            self.pending_by_request.pop(req_id, None)
            self._fail_current_spawn(str(e), job)

    def _ensure_position_definition(self):
        if self.pos_def_id is not None:
            return True
        self.pos_def_id = self.sm.new_def_id()
        err = self.sm.dll.AddToDataDefinition(
            self.sm.hSimConnect,
            self.pos_def_id.value,
            b"Initial Position",
            b"",
            SIMCONNECT_DATATYPE.SIMCONNECT_DATATYPE_INITPOSITION,
            0,
            SIMCONNECT_UNUSED,
        )
        return self.sm.IsHR(err, 0)

    def set_aircraft_position(self, object_id, lat, lon, alt, heading):
        if object_id is None or not self._ensure_position_definition():
            return False

        init_pos = SIMCONNECT_DATA_INITPOSITION(
            Latitude=lat,
            Longitude=lon,
            Altitude=_numeric_alt(alt),
            Pitch=0.0,
            Bank=0.0,
            Heading=float(heading or 0),
            OnGround=0,
            Airspeed=0,
        )

        hr = self.sm.dll.SetDataOnSimObject(
            self.sm.hSimConnect,
            self.pos_def_id.value,
            DWORD(object_id),
            0,
            0,
            sizeof(init_pos),
            pointer(init_pos),
        )
        return self.sm.IsHR(hr, 0)

    def remove_aircraft(self, object_id):
        req_id = self._alloc_request_id()
        hr = self.sm.dll.AIRemoveObject(
            self.sm.hSimConnect,
            DWORD(object_id),
            req_id,
        )
        return self.sm.IsHR(hr, 0)


def msfs_connect():
    dll_path = configure_simconnect_library()
    if not dll_path:
        return None
    try:
        # Must pass library_path explicitly: SimConnect.__init__ default is fixed at import time.
        sm = SimConnect(library_path=dll_path)
        sm._spawn_manager = AISpawnManager(sm)
        dll_name = getattr(sm.dll.SimConnect, "_name", "unknown")
        print(f"[SimConnect] Connected (EX1 AI spawn) DLL={dll_name}")
        return sm
    except Exception as e:
        print(f"[SimConnect] Connection failed: {e}")
        return None

def get_player_position(sm):
    aq = AircraftRequests(sm, _time=2000)
    lat = aq.get("PLANE_LATITUDE")
    lon = aq.get("PLANE_LONGITUDE")
    alt = aq.get("PLANE_ALTITUDE")
    hdg = aq.get("PLANE_HEADING_DEGREES_TRUE")
    return lat, lon, alt, hdg


_last_adsb_data = []

def get_adsb_traffic(lat, lon, radius_km=500):
    global _last_adsb_data

    endpoints = [
        f"https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{radius_km}",
        #f"https://api.adsb.one/v2/point/{lat}/{lon}/{radius_km}",
        #ToDo: Remove the line above, because it was just a test
    ]

    headers = {
        "User-Agent": API_USER_AGENT,
        "Accept": "application/json"
    }

    for url in endpoints:
        try:
            r = requests.get(url, headers=headers, timeout=15)
            
            r.raise_for_status()

            data = r.json()
            aircraft = data.get("ac", [])

            _last_adsb_data = aircraft
            return aircraft

        except Exception as e:
            print(f"[ADS-B] {url} failed: {e}")

    if _last_adsb_data:
        print("[ADS-B] Using cached traffic data")
        return _last_adsb_data

    return []

# =========================
# LANGUAGE DETECTION
# =========================


def detect_language():
    try:
        # Neuer, zukunftssicherer Weg
        lang, _ = locale.getlocale()
        if not lang:
            lang = locale.getdefaultlocale()[0]  # Fallback für ältere Versionen
        lang = lang.lower()

        if lang.startswith("de"):
            return "de"
        if lang.startswith("en"):
            return "en"
        return "en"
    except Exception:
        return "en"


LANG = detect_language()
print("Detected language:", LANG)

# =========================
# TEXT DICTIONARY
# =========================

TEXT = {
    "title": {"de": "MSFS Live Traffic Utility", "en": "MSFS Live Traffic Utility"},
    "flight_label": {"de": "Flugnummer:", "en": "Flight number:"},
    "start": {"de": "Start", "en": "Start"},
    "stop": {"de": "Stop", "en": "Stop"},
    "show_all": {"de": "Alle Flieger anzeigen", "en": "Show all aircraft"},
    "info_title": {"de": "Fluginformationen", "en": "Flight Information"},
    "status_ready": {"de": "Bereit.", "en": "Ready."},
    "tracking_started": {"de": "Tracking gestartet für", "en": "Tracking started for"},
    "tracking_stopped": {"de": "Tracking gestoppt.", "en": "Tracking stopped."},
    "error_no_flight": {"de": "Bitte eine Flugnummer eingeben.", "en": "Please enter a flight number."}
}

# =========================
# HEIGHT GRADIENT (BLUE → YELLOW → RED)
# =========================

def lerp(a, b, t):
    return a + (b - a) * t

def lerp_color(c1, c2, t):
    return (
        int(lerp(c1[0], c2[0], t)),
        int(lerp(c1[1], c2[1], t)),
        int(lerp(c1[2], c2[2], t)),
    )

def altitude_to_color(alt):
    try:
        if alt is None:
            alt = 0
        alt = float(alt)
    except:
        alt = 0

    if alt < 0:
        alt = 0
    if alt > 40000:
        alt = 40000

    BLUE = (0, 0, 255)
    YELLOW = (255, 255, 0)
    RED = (255, 0, 0)

    if alt <= 20000:
        t = alt / 20000
        r, g, b = lerp_color(BLUE, YELLOW, t)
    else:
        t = (alt - 20000) / 20000
        r, g, b = lerp_color(YELLOW, RED, t)

    return f"#{r:02x}{g:02x}{b:02x}"

# =========================
# ADS-B PARSER
# =========================

def parse_adsb(ac: dict):
    return {
        "call": (ac.get("flight") or "").strip().upper(),
        "hex": ac.get("hex"),
        "reg": ac.get("r"),
        "type": ac.get("t"),
        "category": ac.get("category"),

        "lat": ac.get("lat"),
        "lon": ac.get("lon"),

        "alt_baro": ac.get("alt_baro"),
        "alt_geom": ac.get("alt_geom"),

        "gs": ac.get("gs"),
        "track": ac.get("track"),
        "baro_rate": ac.get("baro_rate"),

        "nav_qnh": ac.get("nav_qnh"),
        "nav_alt_mcp": ac.get("nav_altitude_mcp"),

        "squawk": ac.get("squawk"),
        "emergency": ac.get("emergency"),

        "seen": ac.get("seen"),
        "seen_pos": ac.get("seen_pos"),
        "rssi": ac.get("rssi"),

        "dst": ac.get("dst"),
        "dir": ac.get("dir"),

        "nic": ac.get("nic"),
        "rc": ac.get("rc"),
        "version": ac.get("version"),
        "nac_p": ac.get("nac_p"),
        "nac_v": ac.get("nac_v"),
        "sil": ac.get("sil"),
        "sil_type": ac.get("sil_type"),
        "gva": ac.get("gva"),
        "sda": ac.get("sda"),
        "alert": ac.get("alert"),
        "spi": ac.get("spi"),
        "messages": ac.get("messages"),
    }

# =========================
# MSFS AI TRAFFIC (SimConnect spawn + updates)
# =========================

class DummyAircraft:
    STATE_PARKED = "parked"
    STATE_DEPARTING = "departing"
    STATE_AIRBORNE = "airborne"

    def __init__(self, sm, aircraft_title, info, route=None, spawn_mode="airborne", airport_icao=""):
        self.sm = sm
        self.spawn_mgr = getattr(sm, "_spawn_manager", None)
        self.aircraft_title = aircraft_title
        self.callsign = info["call"] or "NXXXX"
        self.route = route
        self.state = spawn_mode if spawn_mode == self.STATE_PARKED else self.STATE_AIRBORNE
        self.airport_icao = (airport_icao or "").upper()
        self.parked_since = time.time() if self.state == self.STATE_PARKED else None

        self.current_lat = info["lat"]
        self.current_lon = info["lon"]
        self.current_alt = _numeric_alt(info["alt_geom"] or info["alt_baro"])

        self.target_lat = self.current_lat
        self.target_lon = self.current_lon
        self.target_alt = self.current_alt

        if route and route.get("dest_lat") is not None:
            self.dest_lat = route["dest_lat"]
            self.dest_lon = route["dest_lon"]
            self.dest_icao = route.get("dest_icao", "")
            self.cruise_alt = max(25000.0, self.current_alt)
        else:
            self.dest_lat = None
            self.dest_lon = None
            self.dest_icao = ""
            self.cruise_alt = max(10000.0, self.current_alt)

        self.track = info["track"] or 0
        self.gs = info["gs"] or 0
        self.rate = info["baro_rate"] or 0

        self.last_seen = time.time()
        self.ai_id = None
        self.spawn_confirmed = False

        if self.spawn_mgr is None:
            print(f"[Dummy ERROR] No spawn manager for {self.callsign}")
            return

        self.spawn_mgr.queue_spawn(
            dummy=self,
            title=self.aircraft_title,
            tail=info.get("reg") or self.callsign,
            lat=self.current_lat,
            lon=self.current_lon,
            alt=self.current_alt,
            heading=self.track,
            ground_speed=info.get("gs") or 0,
            spawn_mode=self.state if self.state == self.STATE_PARKED else "airborne",
            airport_icao=self.airport_icao,
        )

    # -------------------------
    # UPDATE TARGET POSITION
    # -------------------------
    def update_target(self, info, route=None):
        if route:
            self.route = route
            if route.get("dest_lat") is not None:
                self.dest_lat = route["dest_lat"]
                self.dest_lon = route["dest_lon"]
                self.dest_icao = route.get("dest_icao", self.dest_icao)

        self.target_lat = info["lat"]
        self.target_lon = info["lon"]
        self.target_alt = _numeric_alt(info["alt_geom"] or info["alt_baro"]) or self.target_alt

        self.track = info["track"] or self.track
        self.gs = float(info.get("gs") or self.gs or 0)
        self.rate = info.get("baro_rate") or self.rate

        self.last_seen = time.time()

        if self.state == self.STATE_PARKED and self._should_depart():
            self._begin_departure()

    def _should_depart(self):
        if not self.route or self.dest_lat is None:
            return False
        if self.gs >= PARKED_DEPART_GS:
            return True
        if self.rate and float(self.rate) > 200:
            return True
        return False

    def _begin_departure(self):
        self.state = self.STATE_DEPARTING
        self.target_alt = self.cruise_alt
        if self.dest_lat is not None and self.dest_lon is not None:
            self.target_lat = self.dest_lat
            self.target_lon = self.dest_lon
        print(
            f"[Traffic] {self.callsign} departing {self.airport_icao} "
            f"-> {self.dest_icao or 'destination'}"
        )

    def _heading_to(self, lat, lon):
        import math
        dlon = math.radians(lon - self.current_lon)
        lat1 = math.radians(self.current_lat)
        lat2 = math.radians(lat)
        y = math.sin(dlon) * math.cos(lat2)
        x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
        return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0

    def interpolate_and_update(self):
        if self.ai_id is None or self.spawn_mgr is None:
            return

        if self.state == self.STATE_PARKED:
            return

        if self.state == self.STATE_DEPARTING:
            self.target_lat = self.dest_lat if self.dest_lat is not None else self.target_lat
            self.target_lon = self.dest_lon if self.dest_lon is not None else self.target_lon
            self.target_alt = self.cruise_alt
            if self.current_alt >= self.cruise_alt * 0.85 and self.gs > 80:
                self.state = self.STATE_AIRBORNE

        alpha = 0.25
        self.current_lat += alpha * (self.target_lat - self.current_lat)
        self.current_lon += alpha * (self.target_lon - self.current_lon)
        self.current_alt += alpha * (self.target_alt - self.current_alt)

        if self.dest_lat is not None and self.state != self.STATE_PARKED:
            heading = self._heading_to(self.dest_lat, self.dest_lon)
        else:
            heading = self.track or 0

        if not self.spawn_mgr.set_aircraft_position(
            self.ai_id,
            self.current_lat,
            self.current_lon,
            self.current_alt,
            heading,
        ):
            print(f"[Dummy ERROR] Update failed ({self.callsign})")

    # -------------------------
    # DESPAWN LOGIC
    # -------------------------
    def should_despawn(self, timeout=30):
        if self.state == self.STATE_PARKED and self.parked_since:
            if (time.time() - self.parked_since) > MAX_PARKED_WAIT_SEC:
                return True
        return (time.time() - self.last_seen) > timeout

    def despawn(self):
        if self.ai_id is None or self.spawn_mgr is None:
            return
        if self.spawn_mgr.remove_aircraft(self.ai_id):
            print(f"[Dummy] Removed {self.callsign}")
        else:
            print(f"[Dummy ERROR] Remove failed ({self.callsign})")


# =========================
# DUMMY MODE LOOP
# =========================

def _infer_player_airport(plat, plon, traffic, route_cache):
    """Guess player airport ICAO from grounded traffic near the user."""
    if PLAYER_AIRPORT_ICAO:
        return PLAYER_AIRPORT_ICAO

    votes = {}
    for ac in traffic:
        info = parse_adsb(ac)
        if not is_on_ground(info):
            continue
        dist = info.get("dst")
        if dist is None:
            dist = route_haversine_km(plat, plon, info["lat"], info["lon"])
        if dist > AIRPORT_RADIUS_KM:
            continue
        route = route_cache.get(info["call"])
        if not route:
            continue
        o_icao = route["origin_icao"]
        if airport_near(plat, plon, route.get("origin_lat"), route.get("origin_lon")):
            votes[o_icao] = votes.get(o_icao, 0) + 1

    if not votes:
        return ""
    return max(votes, key=votes.get)


def run_msfs_dummy_mode(sm):
    print("MSFS erkannt → AI traffic mode (EX1)")

    model_map = get_model_resolver()
    route_cache = RouteCache()
    dummies = {}

    while True:
        try:
            spawn_mgr = getattr(sm, "_spawn_manager", None)

            plat, plon, palt, phd = get_player_position(sm)
            if spawn_mgr is not None:
                spawn_mgr.set_player_position(plat, plon, palt)
                spawn_mgr.tick()

            traffic = get_adsb_traffic(plat, plon, 80)
            player_airport = _infer_player_airport(plat, plon, traffic, route_cache)
            if spawn_mgr is not None and player_airport:
                if spawn_mgr.player_airport_icao != player_airport:
                    print(f"[Airport] Player airport: {player_airport}")
                spawn_mgr.player_airport_icao = player_airport

            seen_callsigns = set()

            for ac in traffic:
                info = parse_adsb(ac)
                call = info["call"]

                if not call or info["lat"] is None or info["lon"] is None:
                    continue

                dist_km = info.get("dst")
                if dist_km is None:
                    dist_km = _haversine_km(plat, plon, info["lat"], info["lon"])
                if dist_km > MAX_SPAWN_DISTANCE_KM:
                    continue

                route = route_cache.get(call)
                relation = classify_airport_relation(
                    info, route, plat, plon, player_airport
                )

                if is_on_ground(info):
                    if relation == "arriving":
                        if call in dummies:
                            dummies[call].despawn()
                            del dummies[call]
                        continue
                    if relation != "departing" or not player_airport:
                        continue

                seen_callsigns.add(call)

                if not model_map.is_spawnable(info["type"], call):
                    continue

                if call not in dummies and len(dummies) >= MAX_ACTIVE_AI:
                    continue

                aircraft_title = model_map.resolve(info["type"], call)

                spawn_mode = "airborne"
                airport_icao = ""
                if is_on_ground(info) and relation == "departing" and player_airport:
                    spawn_mode = DummyAircraft.STATE_PARKED
                    airport_icao = player_airport

                if call not in dummies:
                    dummies[call] = DummyAircraft(
                        sm,
                        aircraft_title,
                        info,
                        route=route,
                        spawn_mode=spawn_mode,
                        airport_icao=airport_icao,
                    )
                else:
                    dummies[call].update_target(info, route=route)

            for call, dummy in list(dummies.items()):
                if call in seen_callsigns:
                    dummy.interpolate_and_update()
                elif dummy.should_despawn():
                    dummy.despawn()
                    del dummies[call]

            time.sleep(1)

        except Exception as e:
            print("Fehler im MSFS‑Dummy‑Modus:", e)
            time.sleep(2)

# =========================
# GUI MODE (GRADIENT MARKERS + GRADIENT TRAIL S3a)
# =========================

class FlightTrackerApp:
    def __init__(self, root):
        self.root = root
        self.root.title(TEXT["title"][LANG])
        
        # --- FARBEN AUS JSON EXTRAHIEREN ---
        theme = COLOR_SCHEME["gui_theme"]
        bg_main = theme["main_bg"]
        bg_panel = theme["panel_bg"]
        fg_text = theme["text_color"]
        
        # Hauptfenster einfärben
        self.root.config(bg=bg_main)

        # --- TOP BAR ---
        top_frame = tk.Frame(root, bg=bg_main)
        top_frame.pack(side=tk.TOP, fill=tk.X, padx=5, pady=5)
        
        tk.Label(top_frame, text=TEXT["flight_label"][LANG], bg=bg_main, fg=fg_text).pack(side=tk.LEFT)
        
        self.flight_entry = tk.Entry(
            top_frame, width=10, 
            bg=theme["entry_bg"], fg=theme["entry_fg"], 
            insertbackground=theme["text_color"] # Cursor-Farbe
        )
        self.flight_entry.pack(side=tk.LEFT, padx=5)
        self.flight_entry.insert(0, "EWG51Y")
        
        self.start_button = tk.Button(
            top_frame, text=TEXT["start"][LANG], command=self.start_tracking,
            bg=theme["btn_bg"], fg=theme["btn_fg"], activebackground=theme["panel_bg"]
        )
        self.start_button.pack(side=tk.LEFT, padx=5)
        
        self.stop_button = tk.Button(
            top_frame, text=TEXT["stop"][LANG], command=self.stop_tracking, state=tk.DISABLED,
            bg=theme["btn_bg"], fg=theme["btn_fg"], disabledforeground=theme["btn_disabled_bg"]
        )
        self.stop_button.pack(side=tk.LEFT, padx=5)
        
        self.show_all_var = tk.BooleanVar(value=False)
        self.show_all_check = tk.Checkbutton(
            top_frame, text=TEXT["show_all"][LANG], variable=self.show_all_var,
            bg=bg_main, fg=fg_text, selectcolor=bg_panel, activebackground=bg_main, activeforeground=fg_text
        )
        self.show_all_check.pack(side=tk.LEFT, padx=10)

        # --- INFO PANEL ---
        info_frame = tk.Frame(root, relief=tk.GROOVE, borderwidth=2, bg=bg_panel)
        info_frame.pack(side=tk.RIGHT, fill=tk.Y, padx=5, pady=5)
        
        tk.Label(info_frame, text=TEXT["info_title"][LANG], font=("Arial", 12, "bold"), bg=bg_panel, fg=fg_text).pack(pady=5)
        
        self.info_text = tk.Text(
            info_frame, width=35, height=28, state=tk.DISABLED,
            bg=theme["entry_bg"], fg=theme["entry_fg"], wrap=tk.WORD
        )
        self.info_text.pack(padx=5, pady=5)

        # --- STATUS ---
        self.status_label = tk.Label(root, text=TEXT["status_ready"][LANG], anchor="w", bg=bg_main, fg=fg_text)
        self.status_label.pack(side=tk.BOTTOM, fill=tk.X, padx=5, pady=3)

        # --- MAP (Einfärben & Tile Server) ---
        self.map_widget = TkinterMapView(root, width=900, height=650, corner_radius=0)
        self.map_widget.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        # Kartendesign aus JSON laden
        tile_url = COLOR_SCHEME.get("map_settings", {}).get("tile_server_url")
        if tile_url:
            # max_zoom=22 sorgt bei Google-Servern dafür, dass man sehr nah heranzoomen kann
            self.map_widget.set_tile_server(tile_url, max_zoom=22)
            
        self.map_widget.set_position(50.033, 8.570)
        self.map_widget.set_zoom(7)

        # --- STATE ---
        self.tracking = False
        self.tracking_thread = None
        self.markers = {}
        self.selected_flight = None
        self.trail = []
        self.trail_segments = []
        self.trail_points = []
        self.last_seen = {}
        self.DESPAWN_TIMEOUT = 5

    # -------------------------
    # INFO PANEL UPDATE
    # -------------------------
    def update_info_panel(self, info, airline_name, model_name):
        self.info_text.config(state=tk.NORMAL)
        self.info_text.delete("1.0", tk.END)

        self.info_text.insert(tk.END, f"Flight: {info['call']}\n")
        self.info_text.insert(tk.END, f"Airline: {airline_name}\n")
        self.info_text.insert(tk.END, f"ICAO Type: {info['type']}\n")
        self.info_text.insert(tk.END, f"Model: {model_name}\n\n")

        self.info_text.insert(tk.END, f"Hex: {info['hex']}\n")
        self.info_text.insert(tk.END, f"Registration: {info['reg']}\n")
        self.info_text.insert(tk.END, f"Category: {info['category']}\n")
        self.info_text.insert(tk.END, f"Squawk: {info['squawk']}\n")
        self.info_text.insert(tk.END, f"Emergency: {info['emergency']}\n\n")

        self.info_text.insert(tk.END, f"Altitude (baro): {info['alt_baro']} ft\n")
        self.info_text.insert(tk.END, f"Altitude (geom): {info['alt_geom']} ft\n")
        self.info_text.insert(tk.END, f"Speed: {info['gs']} kt\n")
        self.info_text.insert(tk.END, f"Track: {info['track']}°\n")
        self.info_text.insert(tk.END, f"Vertical Rate: {info['baro_rate']} ft/min\n\n")

        self.info_text.insert(tk.END, f"Nav QNH: {info['nav_qnh']}\n")
        self.info_text.insert(tk.END, f"Nav MCP Alt: {info['nav_alt_mcp']} ft\n\n")

        self.info_text.insert(tk.END, f"Lat: {info['lat']}\n")
        self.info_text.insert(tk.END, f"Lon: {info['lon']}\n\n")

        self.info_text.insert(tk.END, f"Seen: {info['seen']} s\n")
        self.info_text.insert(tk.END, f"Seen Pos: {info['seen_pos']} s\n")
        self.info_text.insert(tk.END, f"RSSI: {info['rssi']} dB\n")
        self.info_text.insert(tk.END, f"Distance: {info['dst']} km\n")
        self.info_text.insert(tk.END, f"Direction: {info['dir']}°\n")

        self.info_text.config(state=tk.DISABLED)

    # -------------------------
    # START TRACKING
    # -------------------------
    def start_tracking(self):
        flight = self.flight_entry.get().strip().upper()
        if not flight:
            messagebox.showwarning("Error", TEXT["error_no_flight"][LANG])
            return
        self.selected_flight = flight
        self.tracking = True
        
        # Knopffarben anpassen beim Deaktivieren
        theme = COLOR_SCHEME["gui_theme"]
        self.start_button.config(state=tk.DISABLED, bg=theme["btn_disabled_bg"])
        self.stop_button.config(state=tk.NORMAL, bg=theme["btn_bg"])
        
        self.status_label.config(text=f"{TEXT['tracking_started'][LANG]} {self.selected_flight} …")
    
        # Clear old markers + trail
        for m in self.markers.values():
            m.delete()
        self.markers.clear()

        for seg in self.trail_segments:
            seg.delete()
        self.trail_segments.clear()

        for p in self.trail_points:
            p.delete()
        self.trail_points.clear()

        self.trail.clear()
        self.last_seen.clear()

        self.tracking_thread = threading.Thread(target=self.track_loop, daemon=True)
        self.tracking_thread.start()

    # -------------------------
    # STOP TRACKING
    # -------------------------

    def stop_tracking(self):
        self.tracking = False
        theme = COLOR_SCHEME["gui_theme"]
        self.start_button.config(state=tk.NORMAL, bg=theme["btn_bg"])
        self.stop_button.config(state=tk.DISABLED, bg=theme["btn_disabled_bg"])
        self.status_label.config(text=TEXT["tracking_stopped"][LANG])


    # -------------------------
    # DRAW GRADIENT TRAIL (S3a)
    # -------------------------
    def draw_gradient_trail(self):
        # Remove old
        for seg in self.trail_segments:
            seg.delete()
        self.trail_segments.clear()

        for p in self.trail_points:
            p.delete()
        self.trail_points.clear()

        if len(self.trail) < 2:
            return

        # Draw segments
        for i in range(len(self.trail) - 1):
            lat1, lon1, alt1 = self.trail[i]
            lat2, lon2, alt2 = self.trail[i + 1]

            color = altitude_to_color((alt1 + alt2) / 2)

            seg = self.map_widget.set_path(
                [(lat1, lon1), (lat2, lon2)],
                width=3,
                color=color
            )
            self.trail_segments.append(seg)

        # Draw points
        for lat, lon, alt in self.trail:
            color = altitude_to_color(alt)
            p = self.map_widget.set_marker(
                lat, lon,
                text="",
                marker_color_circle=color,
                marker_color_outside=color
            )
            self.trail_points.append(p)

    # -------------------------
    # TRACK LOOP
    # -------------------------
    def track_loop(self):
        

        while self.tracking:
            center_lat, center_lon = self.map_widget.get_position()
            try:
                aircrafts = get_adsb_traffic(center_lat, center_lon, 250)

                def gui_update():
                    now = time.time()

                    for ac in aircrafts:
                        info = parse_adsb(ac)
                        call = info["call"]

                        if not call or info["lat"] is None or info["lon"] is None:
                            continue

                        self.last_seen[call] = now

                        model_name = get_model_resolver().resolve(info["type"], call)
                        airline_name = call[:3]

                        # Marker color = height gradient
                        color = altitude_to_color(info["alt_baro"])

                        text = f"{call} ({info['type']})\n{airline_name}\n{info['alt_baro']} ft, {info['gs']} kt"

                        # Remove old marker
                        if call in self.markers:
                            self.markers[call].delete()

                        # Only show selected flight unless "show all" is active
                        if not self.show_all_var.get() and call != self.selected_flight:
                            continue

                        # Create marker
                        self.markers[call] = self.map_widget.set_marker(
                            info["lat"], info["lon"],
                            text=text,
                            marker_color_circle=color,
                            marker_color_outside=color
                        )

                        # Update trail for selected flight
                        if call == self.selected_flight:
                            alt = info["alt_geom"] or info["alt_baro"]
                            self.trail.append((info["lat"], info["lon"], alt))
                            self.draw_gradient_trail()
                            self.update_info_panel(info, airline_name, model_name)

                    # Despawn old markers
                    for call in list(self.markers.keys()):
                        last = self.last_seen.get(call, 0)
                        if now - last > self.DESPAWN_TIMEOUT:
                            self.markers[call].delete()
                            del self.markers[call]
                            self.last_seen.pop(call, None)

                    # Center map on selected flight
                    if self.selected_flight in self.markers:
                        m = self.markers[self.selected_flight]
                        self.map_widget.set_position(m.position[0], m.position[1])

                    # Status
                    if self.show_all_var.get():
                        self.status_label.config(
                            text=f"{len(aircrafts)} aircraft received | Trail: {self.selected_flight}"
                        )
                    else:
                        self.status_label.config(
                            text=f"{self.selected_flight} active | {len(aircrafts)} received"
                        )

                self.root.after(0, gui_update)

            except Exception as e:
                self.root.after(0, self.status_label.config, {"text": f"Error: {e}"})

            time.sleep(2)

# =========================
# MAIN
# =========================

def main():
    print("Starte MSFS Live Traffic Utility…")
    print(f"Sprache erkannt: {LANG}")

    try:
        get_model_resolver()
    except FileNotFoundError as e:
        print(f"[ModelMap] ERROR: {e}")
        return

    sm = msfs_connect()

    if sm:
        print("MSFS erkannt → Dummy‑Modus …")
        run_msfs_dummy_mode(sm)
    else:
        print("MSFS nicht erkannt → Starte GUI‑Modus …")
        root = tk.Tk()
        app = FlightTrackerApp(root)
        root.mainloop()


if __name__ == "__main__":
    main()
