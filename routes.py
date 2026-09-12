"""Flight route lookup (adsbdb) and airport proximity helpers."""

import time
import requests

API_USER_AGENT = "MyExtremeFlightTracker/1.0"
ROUTE_API = "https://api.adsbdb.com/v0/callsign/{callsign}"
ROUTE_CACHE_TTL_SEC = 900

AIRPORT_RADIUS_KM = float(__import__("os").environ.get("MSFS_AIRPORT_RADIUS_KM", "8"))


def haversine_km(lat1, lon1, lat2, lon2):
    import math
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


class RouteCache:
    def __init__(self):
        self._cache = {}

    def get(self, callsign):
        call = (callsign or "").strip().upper()
        if not call:
            return None

        cached = self._cache.get(call)
        if cached and (time.time() - cached[1]) < ROUTE_CACHE_TTL_SEC:
            return cached[0]

        route = _fetch_route(call)
        self._cache[call] = (route, time.time())
        return route


def _fetch_route(callsign):
    url = ROUTE_API.format(callsign=requests.utils.quote(callsign.strip()))
    try:
        r = requests.get(
            url,
            headers={"User-Agent": API_USER_AGENT, "Accept": "application/json"},
            timeout=8,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        fr = (data.get("response") or {}).get("flightroute")
        if not fr:
            return None
        origin = fr.get("origin") or {}
        dest = fr.get("destination") or {}
        o_icao = (origin.get("icao_code") or "").upper()
        d_icao = (dest.get("icao_code") or "").upper()
        if not o_icao or not d_icao:
            return None
        return {
            "origin_icao": o_icao,
            "dest_icao": d_icao,
            "origin_lat": origin.get("latitude"),
            "origin_lon": origin.get("longitude"),
            "dest_lat": dest.get("latitude"),
            "dest_lon": dest.get("longitude"),
            "origin_name": origin.get("name"),
            "dest_name": dest.get("name"),
        }
    except (requests.RequestException, ValueError, TypeError):
        return None


def is_on_ground(info, alt_threshold=1500, gs_threshold=40):
    gs = float(info.get("gs") or 0)
    alt = info.get("alt_geom") or info.get("alt_baro")
    if alt == "ground":
        return True
    try:
        alt_f = float(alt or 0)
    except (TypeError, ValueError):
        alt_f = 0
    return gs < gs_threshold and alt_f < alt_threshold


def airport_near(lat, lon, apt_lat, apt_lon, radius_km=AIRPORT_RADIUS_KM):
    if lat is None or lon is None or apt_lat is None or apt_lon is None:
        return False
    return haversine_km(lat, lon, apt_lat, apt_lon) <= radius_km


def classify_airport_relation(info, route, player_lat, player_lon, player_airport_icao):
    """
    Returns: 'departing', 'arriving', 'away', or 'unknown'.
    """
    if not route or player_lat is None or player_lon is None:
        return "unknown"

    o_icao = route["origin_icao"]
    d_icao = route["dest_icao"]
    player_icao = (player_airport_icao or "").upper()

    origin_at_player = airport_near(
        player_lat, player_lon, route.get("origin_lat"), route.get("origin_lon")
    ) or (player_icao and o_icao == player_icao)

    dest_at_player = airport_near(
        player_lat, player_lon, route.get("dest_lat"), route.get("dest_lon")
    ) or (player_icao and d_icao == player_icao)

    ac_lat, ac_lon = info.get("lat"), info.get("lon")
    near_ac_origin = airport_near(ac_lat, ac_lon, route.get("origin_lat"), route.get("origin_lon"))
    near_ac_dest = airport_near(ac_lat, ac_lon, route.get("dest_lat"), route.get("dest_lon"))

    if not is_on_ground(info):
        return "away"

    if dest_at_player and near_ac_dest:
        return "arriving"

    if origin_at_player and near_ac_origin:
        if not player_icao or o_icao == player_icao:
            return "departing"

    return "away"
