from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
import webbrowser

# ==============================================================================
# VERSION & BASIS-KONFIGURATION
# ==============================================================================
APP_VERSION = "3.0.0"
USER_AGENT = f"SimRail-Live-Radar/{APP_VERSION} (Community-Tool)"

CONFIG = {
    "DEFAULT_SERVER": "de1",          # Standard-Server beim ersten Start
    "DEFAULT_WINDOW_MINUTES": 30,     # Standard-Vorschau ab jetzt in Minuten
    "MAX_RESULTS": 12,                # Anzahl der angezeigten freien Züge
    "AUTO_REFRESH_SECONDS": 30,       # Intervall für Auto-Refresh im Browser
    "TIMETABLE_CACHE_SECONDS": 300,   # 5 Min. Cache für große Fahrplandaten (schont API)
    "IDLE_SHUTDOWN_SECONDS": 900,     # Beendet Server nach 15 Min. ohne offenen Browser-Tab
    "PORT": 8765,                     # Lokaler Port
    "RADAR_FALLBACK_KM": 25.0,        # GPS-Nahbereichs-Radar (km)
    "WEIGHT_PLAYER_TRAIN": 1.5,       # Gewichtung pro Spieler-Zug-Begegnung
    "WEIGHT_DISPATCHER": 1.0,         # Gewichtung pro besetztem Stellwerk
}

# Offizielle SimRail Fahrzeug-DLCs (Stand 2026)
DLC_CATALOG = [
    {
        "id": "cargo_pack",
        "patterns": ["et22", "201e"],
        "label": {
            "de": "Cargo Pack DLC (ET22 / 201E)",
            "pl": "Cargo Pack DLC (ET22 / 201E)",
            "en": "Cargo Pack DLC (ET22 / 201E)",
        },
    },
    {
        "id": "impuls2",
        "patterns": ["impuls", "36wed", "ed78"],
        "label": {
            "de": "Impuls 2 DLC (36WEd)",
            "pl": "Impuls 2 DLC (36WEd)",
            "en": "Impuls 2 DLC (36WEd)",
        },
    },
    {
        "id": "cd163",
        "patterns": ["163", "persing", "peršing"],
        "label": {
            "de": "ČD 163 „Peršing“ DLC",
            "pl": "ČD 163 „Peršing” DLC",
            "en": "ČD 163 'Peršing' DLC",
        },
    },
    {
        "id": "br285",
        "patterns": ["br285", "285", "f140"],
        "label": {
            "de": "TRAXX BR 285 Diesel DLC",
            "pl": "TRAXX BR 285 Diesel DLC",
            "en": "TRAXX BR 285 Diesel DLC",
        },
    },
]
# ==============================================================================

BASE_PANEL_URL = "https://panel.simrail.eu:8084"
BASE_AWS_URL = "https://api1.aws.simrail.eu:8082/api"

# Wichtig für PyInstaller (.exe): Dateien neben der .exe speichern, nicht im Temp-Ordner
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

STATE_FILE = os.path.join(BASE_DIR, "simrail_active_train.json")
USER_CONFIG_FILE = os.path.join(BASE_DIR, "simrail_radar_config.json")
PID_FILE = os.path.join(BASE_DIR, "simrail_radar.pid")

TIMETABLE_CACHE = {}
VENDOR_CACHE = {}
TILE_CACHE = {}
DIST_HISTORY = {}
CACHE_LOCK = threading.Lock()
LAST_CLIENT_ACTIVITY = time.time()


def touch_activity():
    global LAST_CLIENT_ACTIVITY
    LAST_CLIENT_ACTIVITY = time.time()


def idle_watchdog():
    """Beendet den Hintergrundprozess automatisch, wenn 15 Min. kein Tab mehr offen war."""
    while True:
        time.sleep(30)
        if time.time() - LAST_CLIENT_ACTIVITY > CONFIG["IDLE_SHUTDOWN_SECONDS"]:
            remove_pid_file()
            os._exit(0)


def http_get_bytes(url, timeout=10):
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


def http_get_json(url, timeout=10):
    raw = http_get_bytes(url, timeout=timeout)
    return json.loads(raw.decode("utf-8"))


def stop_previous_instance():
    if not os.path.exists(PID_FILE):
        return
    try:
        with open(PID_FILE, "r", encoding="utf-8") as f:
            old_pid = int(f.read().strip())
        if old_pid and old_pid != os.getpid():
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(old_pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                os.kill(old_pid, signal.SIGTERM)
            time.sleep(0.3)
    except Exception:
        pass


def write_pid_file():
    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass


def remove_pid_file():
    try:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
    except Exception:
        pass


def default_user_settings():
    return {
        "lang": "auto",
        "server": CONFIG["DEFAULT_SERVER"],
        "window_min": CONFIG["DEFAULT_WINDOW_MINUTES"],
        "priority": "balanced",
        "owned_dlcs": [d["id"] for d in DLC_CATALOG],
        "custom_excluded": [],
    }


def load_user_settings():
    defaults = default_user_settings()
    if os.path.exists(USER_CONFIG_FILE):
        try:
            with open(USER_CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                defaults.update(saved)
        except Exception:
            pass
    return defaults


def save_user_settings(new_settings):
    current = load_user_settings()
    current.update(new_settings)
    try:
        with open(USER_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2, ensure_ascii=False)
    except Exception:
        pass
    return current


def fetch_vendor_asset(asset_key, urls):
    with CACHE_LOCK:
        if asset_key in VENDOR_CACHE:
            return VENDOR_CACHE[asset_key]
    for url in urls:
        try:
            content = http_get_bytes(url, timeout=10)
            if content and len(content) > 1000:
                with CACHE_LOCK:
                    VENDOR_CACHE[asset_key] = content
                return content
        except Exception:
            continue
    return None


def fetch_map_tile(z, y, x):
    key = f"{z}/{y}/{x}"
    with CACHE_LOCK:
        if key in TILE_CACHE:
            return TILE_CACHE[key]
    url = f"https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}"
    try:
        content = http_get_bytes(url, timeout=8)
        if content:
            with CACHE_LOCK:
                if len(TILE_CACHE) > 2000:
                    TILE_CACHE.clear()
                TILE_CACHE[key] = content
            return content
    except Exception:
        pass
    return None


def haversine_km(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2):
        return None
    r = 6371.0
    phi1, phi2 = math.radians(float(lat1)), math.radians(float(lat2))
    dphi = math.radians(float(lat2) - float(lat1))
    dlambda = math.radians(float(lon2) - float(lon1))
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return round(2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a)), 1)


def _valid_pl_coords(lat, lon):
    try:
        la = float(str(lat).replace(",", "."))
        lo = float(str(lon).replace(",", "."))
        if 48.5 <= la <= 55.5 and 13.5 <= lo <= 24.5:
            return (la, lo)
    except (ValueError, TypeError):
        pass
    return None


def extract_live_coords(obj):
    if not isinstance(obj, dict):
        return (None, None)

    candidates = [obj]
    if isinstance(obj.get("TrainData"), dict):
        candidates.insert(0, obj["TrainData"])

    for d in candidates:
        lat = None
        lon = None
        for k, v in d.items():
            if v is None or isinstance(v, (dict, list, bool)):
                continue
            kl = str(k).lower()
            if lat is None and (
                kl in ("latititude", "latititute", "latitude", "lat")
                or "latit" in kl
            ):
                lat = v
            elif lon is None and (
                kl in ("longitute", "longitude", "lon", "lng")
                or "longit" in kl
            ):
                lon = v
        res = _valid_pl_coords(lat, lon)
        if res:
            return res
    return (None, None)


def is_same_station_or_post(occ_clean, point_clean):
    if occ_clean == point_clean:
        return True
    if point_clean.startswith(occ_clean + " "):
        suffix = point_clean[len(occ_clean) + 1 :].strip()
        if len(suffix.split()) == 1 and len(suffix) <= 4:
            return True
    if occ_clean.startswith(point_clean + " "):
        suffix = occ_clean[len(point_clean) + 1 :].strip()
        if len(suffix.split()) == 1 and len(suffix) <= 4:
            return True
    return False


def is_human_driver(train_obj):
    t_data = train_obj.get("TrainData") or {}
    t_type = str(train_obj.get("Type", "")).lower()
    return (
        t_type in ("user", "player")
        or t_data.get("ControlledBySteamID") is not None
        or t_data.get("ControlledByXboxID") is not None
    )


def load_saved_train():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def save_active_train(server_code, train_no):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {"server": server_code.lower(), "train_no": str(train_no).strip()},
                f,
            )
    except Exception:
        pass


def clear_saved_train():
    if os.path.exists(STATE_FILE):
        try:
            os.remove(STATE_FILE)
        except Exception:
            pass


def build_excluded_patterns(user_settings):
    owned = set(user_settings.get("owned_dlcs") or [])
    patterns = []
    for dlc in DLC_CATALOG:
        if dlc["id"] not in owned:
            patterns.extend(p.lower() for p in dlc["patterns"])
    for custom in user_settings.get("custom_excluded") or []:
        c = str(custom).strip().lower()
        if c:
            patterns.append(c)
    return patterns


def is_vehicle_excluded(vehicles_list, excluded_patterns):
    if not excluded_patterns or not vehicles_list:
        return False
    for veh in vehicles_list:
        veh_lower = str(veh).lower()
        if any(ex in veh_lower for ex in excluded_patterns):
            return True
    return False


def parse_simrail_time(time_str):
    if not time_str:
        return None
    try:
        return datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def get_cached_timetables(server_code):
    now = time.time()
    with CACHE_LOCK:
        cached = TIMETABLE_CACHE.get(server_code)
        if cached and (now - cached["ts"] < CONFIG["TIMETABLE_CACHE_SECONDS"]):
            return cached["data"]

    try:
        res_tt = http_get_json(
            f"{BASE_AWS_URL}/getAllTimetables?serverCode={server_code}",
            timeout=14,
        )
    except Exception:
        with CACHE_LOCK:
            if server_code in TIMETABLE_CACHE:
                return TIMETABLE_CACHE[server_code]["data"]
        return {}

    timetables = {}
    if isinstance(res_tt, list):
        for entry in res_tt:
            t_num = str(entry.get("trainNoLocal", "")).strip()
            if not t_num and "timetable" in entry and entry["timetable"]:
                t_num = str(
                    entry["timetable"][0].get("displayedTrainNumber", "")
                ).strip()
            if t_num:
                timetables[t_num] = entry.get("timetable", [])

    with CACHE_LOCK:
        TIMETABLE_CACHE[server_code] = {"ts": now, "data": timetables}
    return timetables


def estimate_simrail_server_now(all_trains, timetables):
    samples = []
    for t in all_trains:
        t_data = t.get("TrainData") or {}
        if t_data.get("Velocity", 0) < 15:
            continue
        t_no = str(t.get("TrainNoLocal", "")).strip()
        cur_idx = t_data.get("VDDelayedTimetableIndex", 0)
        tt = timetables.get(t_no)
        if tt and 0 <= cur_idx < len(tt):
            st = tt[cur_idx]
            dt = parse_simrail_time(st.get("arrivalTime")) or parse_simrail_time(
                st.get("departureTime")
            )
            if dt:
                samples.append(dt)
    if not samples:
        return None
    samples.sort()
    return samples[len(samples) // 2]


def get_train_relative_route(t, timetables, max_minutes=30, server_now=None):
    t_no = str(t.get("TrainNoLocal", "")).strip()
    t_data = t.get("TrainData") or {}
    cur_idx = t_data.get("VDDelayedTimetableIndex", 0)

    tt = timetables.get(t_no)
    if not tt or cur_idx >= len(tt):
        return []

    cur_stop = tt[cur_idx]
    arr_dt = parse_simrail_time(cur_stop.get("arrivalTime"))
    dep_dt = parse_simrail_time(cur_stop.get("departureTime"))

    if server_now and arr_dt and dep_dt and dep_dt > server_now:
        t_base = max(arr_dt, server_now)
    else:
        t_base = dep_dt or arr_dt

    if not t_base:
        return []

    start_idx = max(0, cur_idx - 1)
    max_sec = max_minutes * 60
    rel_points = []

    for idx in range(start_idx, len(tt)):
        p = tt[idx]
        p_arr = parse_simrail_time(p.get("arrivalTime"))
        p_dep = parse_simrail_time(p.get("departureTime"))
        p_time = (p_dep or p_arr) if idx == cur_idx else (p_arr or p_dep)
        if not p_time:
            continue

        rel_sec = (
            0.0 if idx < cur_idx else max(0.0, (p_time - t_base).total_seconds())
        )
        name = (p.get("nameOfPoint") or p.get("nameForPerson") or "").strip()
        if name:
            rel_points.append(
                {
                    "name": name,
                    "rel_sec": rel_sec,
                    "idx": idx,
                    "is_past_anchor": idx < cur_idx,
                }
            )

        if rel_sec > max_sec:
            break

    return rel_points


def has_already_passed(
    train_a, coords_a, dest_a, train_b, coords_b, dest_b, dist_now
):
    if dist_now is None:
        return False

    now = time.time()
    pair_key = tuple(sorted([str(train_a), str(train_b)]))

    with CACHE_LOCK:
        prev = DIST_HISTORY.get(pair_key)
        DIST_HISTORY[pair_key] = {"dist": dist_now, "ts": now}
        if len(DIST_HISTORY) > 25000:
            DIST_HISTORY.clear()

    if prev and (now - prev["ts"]) <= 300:
        if prev["dist"] < 12.0 and dist_now > (prev["dist"] + 0.15):
            return True

    if dist_now < 12.0 and dest_a[0] is not None and dest_b[0] is not None:
        d_a_dest = haversine_km(dest_a[0], dest_a[1], coords_b[0], coords_b[1])
        d_b_dest = haversine_km(dest_b[0], dest_b[1], coords_a[0], coords_a[1])
        if (
            d_a_dest is not None
            and d_b_dest is not None
            and d_a_dest > dist_now
            and d_b_dest > dist_now
        ):
            return True

    return False


def estimate_gps_closing(
    b_train_no,
    b_coords,
    b_speed,
    b_dest_coords,
    p_train_no,
    p_coords,
    p_speed,
    p_dest_coords,
):
    dist_now = haversine_km(b_coords[0], b_coords[1], p_coords[0], p_coords[1])
    if dist_now is None or dist_now > CONFIG["RADAR_FALLBACK_KM"]:
        return None

    if has_already_passed(
        b_train_no,
        b_coords,
        b_dest_coords,
        p_train_no,
        p_coords,
        p_dest_coords,
        dist_now,
    ):
        return None

    b_closing = False
    if b_dest_coords[0] is not None:
        dist_b_dest_to_p = haversine_km(
            b_dest_coords[0], b_dest_coords[1], p_coords[0], p_coords[1]
        )
        if dist_b_dest_to_p is not None and dist_b_dest_to_p < dist_now:
            b_closing = True

    p_closing = False
    if p_dest_coords[0] is not None:
        dist_p_dest_to_b = haversine_km(
            p_dest_coords[0], p_dest_coords[1], b_coords[0], b_coords[1]
        )
        if dist_p_dest_to_b is not None and dist_p_dest_to_b < dist_now:
            p_closing = True

    if (
        (b_closing and p_closing)
        or (b_closing and p_speed < 15)
        or (p_closing and b_speed < 15)
    ):
        combined_speed = max(30.0, float(b_speed) + float(p_speed))
        eta_min = max(1, int(round((dist_now / combined_speed) * 60)))
        kind = "opposite_gps" if (b_closing and p_closing) else "nearby_gps"
        return {
            "eta_min": eta_min,
            "dist_km": dist_now,
            "kind": kind,
        }
    return None


def find_real_encounters_live(
    b_points,
    b_coords,
    b_speed,
    b_dest_coords,
    b_cur_loc,
    player_profiles,
    max_window_sec,
    own_train_no=None,
    max_tolerance_sec=240,
):
    encounters = []
    b_dict = {}
    for p in b_points:
        key = p["name"].lower()
        if key not in b_dict or not p.get("is_past_anchor"):
            b_dict[key] = p
    b_names = list(b_dict.keys())

    for p_prof in player_profiles:
        if own_train_no and p_prof["train_no"] == own_train_no:
            continue

        dist_km = haversine_km(
            b_coords[0],
            b_coords[1],
            p_prof["coords"][0],
            p_prof["coords"][1],
        )

        if has_already_passed(
            own_train_no or "bot",
            b_coords,
            b_dest_coords,
            p_prof["train_no"],
            p_prof["coords"],
            p_prof["dest_coords"],
            dist_km,
        ):
            continue

        p_points = p_prof["points"]
        p_dict = {}
        for p in p_points:
            key = p["name"].lower()
            if key not in p_dict or not p.get("is_past_anchor"):
                p_dict[key] = p
        p_names = list(p_dict.keys())

        shared = [name for name in b_names if name in p_dict]

        matched_meet_sec = None
        kind = ""
        loc_mode = ""
        loc_a = ""
        loc_b = ""

        # Fall 1: Gemeinsamer Streckenabschnitt
        is_opposite = False
        if len(shared) >= 2:
            first_s = shared[0]
            last_s = shared[-1]

            b_first_idx = b_names.index(first_s)
            b_last_idx = b_names.index(last_s)
            p_first_idx = p_names.index(first_s)
            p_last_idx = p_names.index(last_s)

            is_opposite = (
                b_last_idx > b_first_idx and p_last_idx < p_first_idx
            ) or (b_last_idx < b_first_idx and p_last_idx > p_first_idx)

            b_s1, b_s2 = b_dict[first_s]["rel_sec"], b_dict[last_s]["rel_sec"]
            p_s1, p_s2 = p_dict[first_s]["rel_sec"], p_dict[last_s]["rel_sec"]

            if is_opposite:
                b_start_sec, b_end_sec = min(b_s1, b_s2), max(b_s1, b_s2)
                p_start_sec, p_end_sec = min(p_s1, p_s2), max(p_s1, p_s2)
                overlap_start = max(b_start_sec, p_start_sec)
                overlap_end = min(b_end_sec, p_end_sec)

                both_via_past_anchor = (
                    b_dict[first_s].get("is_past_anchor")
                    or p_dict[last_s].get("is_past_anchor")
                )
                already_passed_junction = False
                if (
                    both_via_past_anchor
                    and dist_km is not None
                    and dist_km > 2.5
                    and b_dest_coords[0] is not None
                ):
                    d_b_dest = haversine_km(
                        b_coords[0], b_coords[1], b_dest_coords[0], b_dest_coords[1]
                    )
                    d_p_dest = haversine_km(
                        p_prof["coords"][0],
                        p_prof["coords"][1],
                        b_dest_coords[0],
                        b_dest_coords[1],
                    )
                    if (
                        d_b_dest is not None
                        and d_p_dest is not None
                        and d_b_dest < d_p_dest
                    ):
                        already_passed_junction = True

                if overlap_start <= overlap_end and not already_passed_junction:
                    kind = "opposite"
                    loc_mode = "between"
                    loc_a = b_dict[first_s]["name"]
                    loc_b = b_dict[last_s]["name"]
                    if dist_km is not None and dist_km <= CONFIG["RADAR_FALLBACK_KM"]:
                        closing_v = max(30.0, float(b_speed + p_prof["speed"]))
                        matched_meet_sec = (dist_km / closing_v) * 3600.0
                    else:
                        matched_meet_sec = (overlap_start + overlap_end) / 2.0
            else:
                b_to_dest = (
                    haversine_km(
                        b_coords[0], b_coords[1], b_dest_coords[0], b_dest_coords[1]
                    )
                    if b_dest_coords[0] is not None
                    else None
                )
                p_to_dest = (
                    haversine_km(
                        p_prof["coords"][0],
                        p_prof["coords"][1],
                        b_dest_coords[0],
                        b_dest_coords[1],
                    )
                    if b_dest_coords[0] is not None
                    else None
                )

                live_overtake_pending = False
                already_overtaken = False

                if (
                    dist_km is not None
                    and dist_km <= CONFIG["RADAR_FALLBACK_KM"]
                    and b_to_dest is not None
                    and p_to_dest is not None
                ):
                    if b_speed > p_prof["speed"] + 10:
                        if b_to_dest > p_to_dest + 0.15:
                            live_overtake_pending = True
                        else:
                            already_overtaken = True
                    elif p_prof["speed"] > b_speed + 10:
                        if p_to_dest > b_to_dest + 0.15:
                            live_overtake_pending = True
                        else:
                            already_overtaken = True

                overtake_b_passes_p = (b_s1 >= p_s1) and (b_s2 <= p_s2)
                overtake_p_passes_b = (p_s1 >= b_s1) and (p_s2 <= b_s2)

                if not already_overtaken and (
                    live_overtake_pending or overtake_b_passes_p or overtake_p_passes_b
                ):
                    kind = "overtake"
                    if (
                        dist_km is not None
                        and dist_km <= CONFIG["RADAR_FALLBACK_KM"]
                        and abs(p_prof["speed"] - b_speed) > 10
                    ):
                        closing_v = max(20.0, float(abs(p_prof["speed"] - b_speed)))
                        matched_meet_sec = (dist_km / closing_v) * 3600.0
                        if b_speed < 5 and b_cur_loc:
                            loc_mode = "at"
                            loc_a = b_cur_loc
                        else:
                            loc_mode = "between"
                            loc_a = b_dict[first_s]["name"]
                            loc_b = b_dict[last_s]["name"]
                    else:
                        matched_meet_sec = (max(b_s1, p_s1) + min(b_s2, p_s2)) / 2.0
                        loc_mode = "between"
                        loc_a = b_dict[first_s]["name"]
                        loc_b = b_dict[last_s]["name"]

        # Fall 2: Kreuzung / Halt am selben Bahnhof
        same_corridor_no_overtake = len(shared) >= 2 and not is_opposite
        if matched_meet_sec is None and shared and not same_corridor_no_overtake:
            for s_name in shared:
                b_pt = b_dict[s_name]
                p_pt = p_dict[s_name]
                if b_pt.get("is_past_anchor") or p_pt.get("is_past_anchor"):
                    continue
                b_sec = b_pt["rel_sec"]
                p_sec = p_pt["rel_sec"]
                if abs(b_sec - p_sec) <= max_tolerance_sec:
                    matched_meet_sec = (b_sec + p_sec) / 2.0
                    kind = "opposite" if is_opposite else "crossing"
                    loc_mode = "at"
                    loc_a = b_pt["name"]
                    break

        # Fall 3: GPS-Nahbereichs-Radar
        if matched_meet_sec is None and not same_corridor_no_overtake:
            gps_hit = estimate_gps_closing(
                own_train_no or "bot",
                b_coords,
                b_speed,
                b_dest_coords,
                p_prof["train_no"],
                p_prof["coords"],
                p_prof["speed"],
                p_prof["dest_coords"],
            )
            if gps_hit:
                matched_meet_sec = gps_hit["eta_min"] * 60.0
                kind = gps_hit["kind"]
                loc_mode = "open_line"
                loc_a = p_prof["target"]

        if matched_meet_sec is not None and 0 <= matched_meet_sec <= max_window_sec:
            encounters.append(
                {
                    "train_no": p_prof["train_no"],
                    "vehicle": p_prof["vehicle"],
                    "kind": kind,
                    "loc_mode": loc_mode,
                    "loc_a": loc_a,
                    "loc_b": loc_b,
                    "eta_min": max(1, int(round(matched_meet_sec / 60))),
                    "dist_km": dist_km,
                    "coords": p_prof["coords"],
                }
            )

    encounters.sort(
        key=lambda e: (
            e["eta_min"],
            e["dist_km"] if e["dist_km"] is not None else 9999.0,
        )
    )
    return encounters


def evaluate_single_train(
    train_obj,
    timetables,
    occupied_stations,
    station_coords_lookup,
    player_profiles,
    window_min,
    server_now=None,
    w_train=1.0,
    w_disp=1.0,
):
    t_no = str(train_obj.get("TrainNoLocal", "")).strip()
    t_points = get_train_relative_route(
        train_obj, timetables, max_minutes=window_min, server_now=server_now
    )
    if not t_points:
        return None

    max_window_sec = window_min * 60

    dispatched_on_route = []
    seen_stations = set()
    all_route_names = {p["name"].lower() for p in t_points}

    for p in t_points:
        if p.get("is_past_anchor"):
            continue
        if not (0 <= p["rel_sec"] <= max_window_sec):
            continue
        p_lower = p["name"].lower()

        matched_infos = [
            occ_info
            for occ_clean, occ_info in occupied_stations.items()
            if occ_clean == p_lower
        ]

        if not matched_infos:
            matched_infos = [
                occ_info
                for occ_clean, occ_info in occupied_stations.items()
                if occ_clean not in all_route_names
                and is_same_station_or_post(occ_clean, p_lower)
            ]

        for occ_info in matched_infos:
            orig_name = occ_info["name"]
            if orig_name not in seen_stations:
                seen_stations.add(orig_name)
                dispatched_on_route.append(
                    {
                        "name": orig_name,
                        "eta_min": max(1, int(round(p["rel_sec"] / 60))),
                        "rel_sec": p["rel_sec"],
                        "idx": p["idx"],
                        "coords": occ_info["coords"],
                    }
                )
    dispatched_on_route.sort(key=lambda d: (d["rel_sec"], d["idx"]))

    t_coords = extract_live_coords(train_obj)
    t_data = train_obj.get("TrainData") or {}
    cur_speed = int(round(t_data.get("Velocity", 0)))
    end_station = train_obj.get("EndStation", "")
    dest_coords = station_coords_lookup.get(
        end_station.strip().lower(), (None, None)
    )

    tt_entry = timetables.get(t_no, [])
    cur_idx = t_data.get("VDDelayedTimetableIndex", 0)
    cur_loc = (
        tt_entry[cur_idx].get("nameOfPoint")
        if cur_idx < len(tt_entry)
        else train_obj.get("StartStation")
    )

    player_encounters = find_real_encounters_live(
        t_points,
        t_coords,
        cur_speed,
        dest_coords,
        cur_loc,
        player_profiles,
        max_window_sec=max_window_sec,
        own_train_no=t_no,
    )

    veh = train_obj.get("Vehicles", ["Unbekannt"])
    veh_name = veh[0] if veh else "Unbekannt"

    total_interactions = len(player_encounters) + len(dispatched_on_route)
    rank_weight = (
        len(player_encounters) * w_train
        + len(dispatched_on_route) * w_disp
    )

    return {
        "train_no": t_no,
        "name": train_obj.get("TrainName", ""),
        "vehicle": veh_name,
        "current_pos": cur_loc,
        "target": end_station,
        "speed": cur_speed,
        "coords": t_coords,
        "dispatched": dispatched_on_route,
        "encounters": player_encounters,
        "total_interactions": total_interactions,
        "rank_weight": rank_weight,
    }


def fetch_dashboard_payload(requested_server=None, requested_window=None):
    touch_activity()
    user_settings = load_user_settings()
    saved = load_saved_train()

    server_code = (
        requested_server
        or (saved.get("server") if saved else None)
        or user_settings.get("server")
        or CONFIG["DEFAULT_SERVER"]
    ).lower()

    try:
        window_min = max(
            5,
            min(
                180,
                int(
                    requested_window
                    if requested_window is not None
                    else user_settings.get("window_min", CONFIG["DEFAULT_WINDOW_MINUTES"])
                ),
            ),
        )
    except (TypeError, ValueError):
        window_min = CONFIG["DEFAULT_WINDOW_MINUTES"]

    if requested_server or requested_window:
        save_user_settings({"server": server_code, "window_min": window_min})

    tracked_train_no = (
        saved.get("train_no")
        if saved and saved.get("server") == server_code
        else None
    )

    servers = []
    try:
        r_srv = http_get_json(f"{BASE_PANEL_URL}/servers-open", timeout=6)
        servers = [
            {
                "code": s["ServerCode"].lower(),
                "name": s.get("ServerName", s["ServerCode"]),
            }
            for s in r_srv.get("data", [])
            if s.get("ServerCode") and s.get("IsOnline", True)
        ]
    except Exception:
        servers = [
            {"code": "de1", "name": "DE1 (Deutsch)"},
            {"code": "pl1", "name": "PL1 (Polski)"},
            {"code": "en1", "name": "EN1 (English)"},
        ]

    occupied_stations = {}
    station_coords_lookup = {}
    map_stations = []
    res_stations = http_get_json(
        f"{BASE_PANEL_URL}/stations-open?serverCode={server_code}",
        timeout=8,
    )
    for s in res_stations.get("data", []):
        coords = extract_live_coords(s)
        clean_name = s.get("Name", "").strip().lower()
        if coords[0] is not None:
            station_coords_lookup[clean_name] = coords
        dispatchers = s.get("DispatchedBy") or []
        if len(dispatchers) > 0:
            occupied_stations[clean_name] = {
                "name": s.get("Name"),
                "coords": coords,
            }
            if coords[0] is not None:
                map_stations.append({"name": s.get("Name"), "coords": coords})

    res_trains = http_get_json(
        f"{BASE_PANEL_URL}/trains-open?serverCode={server_code}",
        timeout=8,
    )
    all_trains = res_trains.get("data", [])

    excluded_patterns = build_excluded_patterns(user_settings)
    priority = user_settings.get("priority", "balanced")
    if priority == "trains":
        w_train, w_disp = 2.5, 1.0
    elif priority == "dispatchers":
        w_train, w_disp = 1.0, 2.5
    else:
        priority = "balanced"
        w_train, w_disp = 1.0, 1.0

    player_trains = []
    bot_trains = []
    tracked_train_obj = None
    excluded_count = 0

    for t in all_trains:
        t_no = str(t.get("TrainNoLocal", "")).strip()
        if tracked_train_no and t_no == tracked_train_no:
            tracked_train_obj = t

        if is_human_driver(t):
            player_trains.append(t)
        else:
            if is_vehicle_excluded(t.get("Vehicles", []), excluded_patterns):
                excluded_count += 1
                continue
            bot_trains.append(t)

    timetables = get_cached_timetables(server_code)
    server_now = estimate_simrail_server_now(all_trains, timetables)

    player_profiles = []
    map_players = []
    for pt in player_trains:
        p_no = str(pt.get("TrainNoLocal", "")).strip()
        p_veh = pt.get("Vehicles", ["Lok"])[0]
        p_coords = extract_live_coords(pt)
        p_speed = int(round((pt.get("TrainData") or {}).get("Velocity", 0)))
        p_target = pt.get("EndStation", "")
        p_dest_coords = station_coords_lookup.get(
            p_target.strip().lower(), (None, None)
        )
        p_points = get_train_relative_route(
            pt, timetables, max_minutes=window_min, server_now=server_now
        )
        if p_points or p_coords[0] is not None:
            player_profiles.append(
                {
                    "train_no": p_no,
                    "name": pt.get("TrainName", ""),
                    "vehicle": p_veh,
                    "target": p_target,
                    "speed": p_speed,
                    "points": p_points,
                    "coords": p_coords,
                    "dest_coords": p_dest_coords,
                }
            )
        if p_coords[0] is not None and p_no != tracked_train_no:
            map_players.append(
                {
                    "train_no": p_no,
                    "vehicle": p_veh,
                    "target": p_target,
                    "speed": p_speed,
                    "coords": p_coords,
                }
            )

    active_eval = None
    active_missing = False
    if tracked_train_no:
        if tracked_train_obj:
            active_eval = evaluate_single_train(
                tracked_train_obj,
                timetables,
                occupied_stations,
                station_coords_lookup,
                player_profiles,
                window_min=window_min,
                server_now=server_now,
                w_train=w_train,
                w_disp=w_disp,
            )
        else:
            active_missing = True
            clear_saved_train()

    candidates = []
    for bt in bot_trains:
        res = evaluate_single_train(
            bt,
            timetables,
            occupied_stations,
            station_coords_lookup,
            player_profiles,
            window_min=window_min,
            server_now=server_now,
            w_train=w_train,
            w_disp=w_disp,
        )
        if res and res["total_interactions"] > 0:
            candidates.append(res)

    candidates.sort(key=lambda x: (-x["rank_weight"], -x["total_interactions"]))

    return {
        "version": APP_VERSION,
        "timestamp": datetime.now().strftime("%H:%M:%S"),
        "pid": os.getpid(),
        "server": server_code,
        "servers": servers,
        "config": {
            "window_min": window_min,
            "refresh_sec": CONFIG["AUTO_REFRESH_SECONDS"],
            "w_train": w_train,
            "w_disp": w_disp,
            "priority": priority,
            "lang": user_settings.get("lang", "auto"),
            "owned_dlcs": user_settings.get("owned_dlcs", []),
            "custom_excluded": user_settings.get("custom_excluded", []),
            "dlc_catalog": DLC_CATALOG,
        },
        "stats": {
            "stations": len(occupied_stations),
            "players": len(player_trains),
            "available_bots": len(bot_trains),
            "excluded_bots": excluded_count,
        },
        "tracked_train_no": tracked_train_no if not active_missing else None,
        "active_missing": active_missing,
        "active_train": active_eval,
        "candidates": candidates[: CONFIG["MAX_RESULTS"]],
        "map": {
            "stations": map_stations,
            "players": map_players,
        },
    }


HTML_PAGE = """<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>SimRail Live Radar</title>
  <link rel="stylesheet" href="/vendor/leaflet.css" />
  <script src="/vendor/leaflet.js"></script>
  <style>
    :root {
      --bg: #0f1117;
      --panel: #181b23;
      --card: #1f2430;
      --border: #2d3446;
      --text: #e6e9f0;
      --muted: #8b95a5;
      --accent: #3b82f6;
      --accent-hover: #2563eb;
      --green: #22c55e;
      --purple: #a855f7;
      --red: #ef4444;
      --gold: #eab308;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.45;
    }
    header {
      background: var(--panel);
      border-bottom: 1px solid var(--border);
      padding: 12px 24px;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      position: sticky;
      top: 0;
      z-index: 1000;
    }
    .brand { display: flex; align-items: center; gap: 10px; }
    .brand h1 { font-size: 1.12rem; font-weight: 700; letter-spacing: 0.3px; }
    .badge-live {
      background: rgba(34, 197, 94, 0.15);
      color: var(--green);
      border: 1px solid rgba(34, 197, 94, 0.4);
      padding: 2px 8px;
      border-radius: 99px;
      font-size: 0.74rem;
      font-weight: 600;
    }
    .controls { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
    .window-control {
      display: flex;
      align-items: center;
      gap: 6px;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 4px 10px;
      font-size: 0.84rem;
      color: var(--muted);
    }
    .window-control input {
      width: 52px;
      padding: 3px 6px;
      text-align: center;
      border: 1px solid var(--border);
      background: #13161e;
      color: var(--text);
      border-radius: 4px;
      font-weight: 600;
    }
    .lang-switch {
      display: inline-flex;
      background: #13161e;
      border: 1px solid var(--border);
      border-radius: 6px;
      overflow: hidden;
    }
    .lang-btn {
      border: none;
      background: transparent;
      color: var(--muted);
      padding: 6px 9px;
      font-size: 0.78rem;
      font-weight: 700;
      border-radius: 0;
    }
    .lang-btn.active {
      background: var(--accent);
      color: #fff;
    }
    select, input[type="text"], button {
      background: var(--card);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 7px 11px;
      font-size: 0.86rem;
      outline: none;
    }
    select:focus, input[type="text"]:focus { border-color: var(--accent); }
    button { cursor: pointer; font-weight: 600; transition: 0.15s; }
    button:hover { border-color: var(--accent); }
    .btn-primary { background: var(--accent); border-color: var(--accent); color: #fff; }
    .btn-primary:hover { background: var(--accent-hover); }
    .btn-danger { background: rgba(239, 68, 68, 0.15); border-color: rgba(239, 68, 68, 0.4); color: #fca5a5; }
    .btn-danger:hover { background: rgba(239, 68, 68, 0.25); }

    .stats-bar {
      display: flex;
      flex-wrap: wrap;
      gap: 18px;
      padding: 10px 24px;
      background: #13161e;
      border-bottom: 1px solid var(--border);
      font-size: 0.84rem;
      color: var(--muted);
    }
    .stats-bar strong { color: var(--text); }

    .layout {
      display: grid;
      grid-template-columns: 1.25fr 0.95fr;
      gap: 20px;
      padding: 20px 24px;
      max-width: 1650px;
      margin: 0 auto;
    }
    @media (max-width: 1100px) {
      .layout { grid-template-columns: 1fr; }
    }

    .section-title {
      font-size: 0.88rem;
      text-transform: uppercase;
      letter-spacing: 0.8px;
      color: var(--muted);
      margin-bottom: 10px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }

    .active-box {
      background: linear-gradient(145deg, #1b2232, #181d29);
      border: 1px solid #35476b;
      border-radius: 10px;
      padding: 16px;
      margin-bottom: 22px;
    }
    .active-bar {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 12px;
    }
    .active-bar input { width: 180px; }

    .train-card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 15px 16px;
      margin-bottom: 12px;
      transition: transform 0.1s, border-color 0.15s;
      cursor: pointer;
    }
    .train-card:hover { border-color: #4b5878; }
    .train-card.selected { border-color: var(--green); box-shadow: 0 0 0 1px rgba(34, 197, 94, 0.35); }

    .card-header {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 10px;
      margin-bottom: 8px;
    }
    .train-title { font-size: 1.04rem; font-weight: 700; }
    .train-veh { color: var(--muted); font-weight: 400; font-size: 0.88rem; }
    .pill {
      display: inline-block;
      padding: 2px 8px;
      border-radius: 5px;
      font-size: 0.77rem;
      font-weight: 600;
    }
    .pill-count { background: rgba(59, 130, 246, 0.18); color: #93c5fd; border: 1px solid rgba(59, 130, 246, 0.35); }
    .pill-stop { background: rgba(34, 197, 94, 0.15); color: #86efac; border: 1px solid rgba(34, 197, 94, 0.35); }
    .pill-move { background: rgba(245, 158, 11, 0.15); color: #fcd34d; border: 1px solid rgba(245, 158, 11, 0.35); }

    .route-line { font-size: 0.88rem; color: #cbd5e1; margin-bottom: 10px; }
    .detail-group { margin-top: 8px; font-size: 0.85rem; }
    .detail-label { color: var(--muted); font-size: 0.76rem; text-transform: uppercase; letter-spacing: 0.4px; margin-bottom: 4px; }
    .fdl-tags { display: flex; flex-wrap: wrap; gap: 6px; }
    .fdl-tag {
      background: rgba(234, 179, 8, 0.14);
      border: 1px solid rgba(234, 179, 8, 0.45);
      color: #fde047;
      padding: 2px 8px;
      border-radius: 4px;
      font-size: 0.81rem;
    }
    .enc-list { list-style: none; display: flex; flex-direction: column; gap: 4px; }
    .enc-item {
      background: rgba(15, 17, 23, 0.55);
      border-left: 3px solid var(--purple);
      padding: 5px 9px;
      border-radius: 0 5px 5px 0;
      font-size: 0.83rem;
    }
    .enc-item.opposite { border-left-color: var(--red); }
    .enc-meta { color: var(--muted); font-size: 0.79rem; }

    .map-sticky { position: sticky; top: 74px; }
    #map {
      height: calc(100vh - 165px);
      min-height: 460px;
      border-radius: 10px;
      border: 1px solid var(--border);
      background: #12151c;
    }
    .legend {
      display: flex;
      flex-wrap: wrap;
      gap: 14px;
      margin-top: 10px;
      font-size: 0.79rem;
      color: var(--muted);
    }
    .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 5px; }
    .diamond { display: inline-block; width: 9px; height: 9px; transform: rotate(45deg); margin-right: 6px; border: 1px solid #000; }

    /* Modal für DLC-Auswahl */
    .modal-backdrop {
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(0, 0, 0, 0.65);
      z-index: 2000;
      align-items: center;
      justify-content: center;
      padding: 20px;
    }
    .modal-backdrop.open { display: flex; }
    .modal-box {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 12px;
      max-width: 480px;
      width: 100%;
      padding: 20px;
      box-shadow: 0 20px 40px rgba(0,0,0,0.5);
    }
    .modal-title { font-size: 1.05rem; font-weight: 700; margin-bottom: 6px; }
    .modal-sub { font-size: 0.84rem; color: var(--muted); margin-bottom: 14px; }
    .dlc-list { display: flex; flex-direction: column; gap: 9px; margin-bottom: 14px; }
    .dlc-item {
      display: flex;
      align-items: center;
      gap: 10px;
      background: var(--card);
      border: 1px solid var(--border);
      padding: 9px 12px;
      border-radius: 8px;
      cursor: pointer;
      font-size: 0.88rem;
    }
    .dlc-item input { width: 16px; height: 16px; accent-color: var(--accent); cursor: pointer; }
    .modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 14px; }
  </style>
</head>
<body>

<header>
  <div class="brand">
    <h1 id="ui-title">SimRail Zug- & Interaktions-Radar</h1>
    <span class="badge-live" id="status-badge">LIVE</span>
  </div>
  <div class="controls">
    <div class="lang-switch" title="Language / Język / Sprache">
      <button class="lang-btn" id="lang-pl" onclick="setLang('pl')">PL</button>
      <button class="lang-btn" id="lang-de" onclick="setLang('de')">DE</button>
      <button class="lang-btn" id="lang-en" onclick="setLang('en')">EN</button>
    </div>
    <div class="window-control">
      <span id="ui-window-label">Vorschau:</span>
      <input type="number" id="window-input" min="5" max="180" step="5" value="30" onchange="loadData()" />
      <span id="ui-window-unit">Min.</span>
    </div>
    <select id="server-select" onchange="changeServer()"></select>
    <select id="priority-select" onchange="changePriority()" title="Priorität für Zug-Empfehlungen">
      <option value="balanced" id="opt-prio-balanced">Fokus: Egal (Ausgewogen)</option>
      <option value="trains" id="opt-prio-trains">Fokus: Mehr Spieler-Züge</option>
      <option value="dispatchers" id="opt-prio-dispatchers">Fokus: Mehr Stellwerke (Fdl)</option>
    </select>
    <button onclick="openDlcModal()" id="btn-dlc">DLCs</button>
    <button onclick="toggleAutoRefresh()" id="btn-auto">Auto-Refresh: AN (30s)</button>
    <button class="btn-primary" onclick="loadData()" id="btn-refresh">Jetzt aktualisieren</button>
    <button class="btn-danger" onclick="shutdownServer()" id="btn-exit">Beenden</button>
  </div>
</header>

<div class="stats-bar" id="stats-bar">...</div>

<div class="layout">
  <div>
    <div class="section-title">
      <span id="ui-my-train-title">Mein aktueller Zug</span>
    </div>
    <div class="active-box">
      <div class="active-bar">
        <input type="text" id="active-input" placeholder="Zugnummer (z. B. 41026)" onkeydown="if(event.key==='Enter') setActiveTrain()" />
        <button class="btn-primary" onclick="setActiveTrain()" id="btn-track">Zug tracken</button>
        <button class="btn-danger" id="btn-clear-active" style="display:none;" onclick="clearActiveTrain()">Beenden / Freigeben</button>
      </div>
      <div id="active-train-content" style="color: var(--muted); font-size: 0.88rem;"></div>
    </div>

    <div class="section-title">
      <span id="candidates-header">Empfohlene freie Züge</span>
      <span id="last-update" style="font-size: 0.78rem;"></span>
    </div>
    <div id="candidates-list"></div>
  </div>

  <div class="map-sticky">
    <div class="section-title">
      <span id="map-title">Live-Karte</span>
    </div>
    <div id="map"></div>
    <div class="legend" id="map-legend"></div>
  </div>
</div>

<!-- DLC Modal -->
<div class="modal-backdrop" id="dlc-modal" onclick="if(event.target===this) closeDlcModal()">
  <div class="modal-box">
    <div class="modal-title" id="dlc-modal-title">Meine Fahrzeug-DLCs</div>
    <div class="modal-sub" id="dlc-modal-sub">Hake an, welche DLCs du besitzt. Nicht angehakte Fahrzeuge werden bei den freien Zug-Empfehlungen automatisch ausgeblendet.</div>
    <div class="dlc-list" id="dlc-checkboxes"></div>
    <div style="margin-top:8px;">
      <div class="detail-label" id="dlc-custom-label">Zusätzliche Fahrzeugfilter (kommagetrennt, optional):</div>
      <input type="text" id="dlc-custom-input" style="width:100%;margin-top:4px;" placeholder="z. B. EN57, EU07" />
    </div>
    <div class="modal-actions">
      <button onclick="closeDlcModal()" id="btn-dlc-cancel">Abbrechen</button>
      <button class="btn-primary" onclick="saveDlcSettings()" id="btn-dlc-save">Speichern</button>
    </div>
  </div>
</div>

<script>
  const I18N = {
    de: {
      title: "SimRail Zug- & Interaktions-Radar",
      windowLabel: "Vorschau:",
      windowUnit: "Min.",
      dlcBtn: "DLCs",
      autoOn: "Auto-Refresh: AN ({s}s)",
      autoPause: "Auto-Refresh: PAUSE",
      refreshNow: "Jetzt aktualisieren",
      exitBtn: "Beenden",
      loading: "LÄDT...",
      live: "LIVE",
      error: "FEHLER",
      statServer: "Server",
      statStations: "Besetzte Stellwerke",
      statPlayers: "Spieler-Züge",
      statBots: "Verfügbare Züge",
      statFiltered: "{n} durch DLC-Filter ausgeblendet",
      statWindow: "Zeitraum: Ab jetzt für {m} Min.",
      myTrainTitle: "Mein aktueller Zug",
      trainPlaceholder: "Zugnummer (z. B. 41026)",
      trackBtn: "Zug tracken",
      releaseBtn: "Beenden / Freigeben",
      noActiveTrain: "Aktuell ist kein eigener Zug hinterlegt. Klicke unten auf „Zug übernehmen“ oder gib oben deine Zugnummer ein.",
      activeMissing: "Der gespeicherte Zug ist auf {srv} nicht mehr aktiv (Ziel erreicht).",
      emptyAheadWarn: "⚠️ Vor dir ist aktuell alles leer. Ein Wechsel auf einen Zug unten lohnt sich.",
      topTrainsHeader: "Top freie Züge (Gewichtung: Zug ×{wt} / Stellwerk ×{wd})",
      updatedAt: "Stand: {t} (PID {pid})",
      noCandidates: "Keine freien Züge mit Spieler-Interaktionen im Zeitfenster gefunden.",
      takeTrainBtn: "Zug übernehmen",
      trainWord: "Zug",
      stopBadge: "HALT AM BAHNSTEIG (0 km/h)",
      moveBadge: "Fährt ({v} km/h)",
      interactionsCount: "{n} Interaktionen",
      dispatchedHeader: "Stellwerke mit Spielern ({n})",
      dispatchedNone: "Stellwerke mit Spielern: Keine (nur KI)",
      inApproxMin: "in ~{m} Min.",
      inCaMin: "in ca. {m} Min.",
      distAway: "aktuell {d} km entfernt",
      encountersHeader: "Spieler-Begegnungen ({n})",
      encountersNone: "Spieler-Züge: Keine Begegnung im Zeitfenster",
      kind_opposite: "Gegenverkehr",
      kind_opposite_gps: "Gegenverkehr (GPS-Radar)",
      kind_overtake: "Gleiche Richtung (Überholung)",
      kind_crossing: "Kreuzung/Bahnhof",
      kind_nearby_gps: "Nahbereich (GPS-Radar)",
      loc_between: "zwischen {a} und {b}",
      loc_at: "in/bei {a}",
      loc_open_line: "auf freier Strecke (Richtung {a})",
      mapDefaultTitle: "Live-Karte (Klick auf Zug zeigt Begegnungen)",
      mapFocusTitle: "Live-Karte • Fokus auf Zug {no} ({pos})",
      legFocus: "Gewählter / Aktiver Zug",
      legOpposite: "Gegenverkehr (Spieler)",
      legOvertake: "Gleiche Richtung / Kreuzung",
      legOther: "Sonstiger Spieler-Zug",
      legStation: "Besetztes Stellwerk (Fdl)",
      popupStation: "Stellwerk: {name}",
      popupStationSub: "Besetzt durch Spieler",
      popupPlayer: "Spieler-Zug {no}",
      popupTarget: "Ziel: {t}",
      popupFocus: "Fokus: Zug {no}",
      popupAt: "Bei: {pos}",
      prioBalanced: "Fokus: Egal (Ausgewogen)",
      prioTrains: "Fokus: Mehr Spieler-Züge",
      prioDispatchers: "Fokus: Mehr Stellwerke (Fdl)",
      dlcTitle: "Meine Fahrzeug-DLCs",
      dlcSub: "Hake an, welche DLCs du besitzt. Nicht angehakte Fahrzeuge werden bei den freien Zug-Empfehlungen automatisch ausgeblendet.",
      dlcCustomLabel: "Zusätzliche Fahrzeugfilter (kommagetrennt, optional):",
      cancel: "Abbrechen",
      save: "Speichern",
      shutdownTitle: "SimRail Radar vollständig beendet",
      shutdownSub: "Der Hintergrundprozess wurde gestoppt. Du kannst diesen Tab schließen."
    },
    pl: {
      title: "SimRail Radar Pociągów i Interakcji",
      windowLabel: "Podgląd:",
      windowUnit: "min",
      dlcBtn: "DLC",
      autoOn: "Auto-odśw.: WŁ. ({s}s)",
      autoPause: "Auto-odśw.: PAUZA",
      refreshNow: "Odśwież teraz",
      exitBtn: "Zakończ",
      loading: "ŁADOWANIE...",
      live: "NA ŻYWO",
      error: "BŁĄD",
      statServer: "Serwer",
      statStations: "Obsadzone nastawnie",
      statPlayers: "Pociągi graczy",
      statBots: "Dostępne pociągi (AI)",
      statFiltered: "{n} ukrytych przez filtr DLC",
      statWindow: "Zakres: od teraz przez {m} min",
      myTrainTitle: "Mój aktualny pociąg",
      trainPlaceholder: "Numer pociągu (np. 41026)",
      trackBtn: "Śledź pociąg",
      releaseBtn: "Zwolnij / Wyczyść",
      noActiveTrain: "Brak wybranego pociągu. Kliknij „Przejmij pociąg” poniżej lub wpisz swój numer pociągu powyżej.",
      activeMissing: "Zapisany pociąg nie jest już aktywny na serwerze {srv} (dotarł do celu).",
      emptyAheadWarn: "⚠️ Przed Tobą brak graczy i dyżurnych. Warto rozważyć przesiadkę na pociąg z listy poniżej.",
      topTrainsHeader: "Polecane wolne pociągi (waga: pociąg ×{wt} / nastawnia ×{wd})",
      updatedAt: "Stan: {t} (PID {pid})",
      noCandidates: "Brak wolnych pociągów z interakcjami w wybranym oknie czasowym.",
      takeTrainBtn: "Przejmij pociąg",
      trainWord: "Pociąg",
      stopBadge: "POSTÓJ NA STACJI (0 km/h)",
      moveBadge: "W ruchu ({v} km/h)",
      interactionsCount: "{n} interakcji",
      dispatchedHeader: "Nastawnie z graczami ({n})",
      dispatchedNone: "Nastawnie z graczami: Brak (tylko AI)",
      inApproxMin: "za ~{m} min",
      inCaMin: "za ok. {m} min",
      distAway: "obecnie {d} km stąd",
      encountersHeader: "Spotkania z graczami ({n})",
      encountersNone: "Pociągi graczy: Brak spotkań w wybranym czasie",
      kind_opposite: "Ruch przeciwny",
      kind_opposite_gps: "Ruch przeciwny (GPS)",
      kind_overtake: "Ten sam kierunek (Wyprzedzanie)",
      kind_crossing: "Krzyżowanie / Stacja",
      kind_nearby_gps: "W pobliżu (GPS)",
      loc_between: "między {a} a {b}",
      loc_at: "w/przy {a}",
      loc_open_line: "na szlaku (kierunek {a})",
      mapDefaultTitle: "Mapa na żywo (kliknij pociąg, aby zobaczyć spotkania)",
      mapFocusTitle: "Mapa na żywo • Fokus: pociąg {no} ({pos})",
      legFocus: "Wybrany / aktywny pociąg",
      legOpposite: "Ruch przeciwny (gracz)",
      legOvertake: "Ten sam kierunek / krzyżowanie",
      legOther: "Inny pociąg gracza",
      legStation: "Obsadzona nastawnia (dyżurny)",
      popupStation: "Nastawnia: {name}",
      popupStationSub: "Obsadzona przez gracza",
      popupPlayer: "Pociąg gracza {no}",
      popupTarget: "Cel: {t}",
      popupFocus: "Fokus: Pociąg {no}",
      popupAt: "Lokalizacja: {pos}",
      prioBalanced: "Priorytet: Obojętnie (Równowaga)",
      prioTrains: "Priorytet: Więcej pociągów graczy",
      prioDispatchers: "Priorytet: Więcej nastawni (dyżurni)",
      dlcTitle: "Moje dodatki DLC (Tabor)",
      dlcSub: "Zaznacz posiadane dodatki DLC. Niezaznaczone pojazdy zostaną automatycznie ukryte na liście rekomendacji.",
      dlcCustomLabel: "Dodatkowy filtr pojazdów (oddzielone przecinkami, opcjonalnie):",
      cancel: "Anuluj",
      save: "Zapisz",
      shutdownTitle: "SimRail Radar został zamknięty",
      shutdownSub: "Proces w tle został zatrzymany. Możesz zamknąć tę kartę."
    },
    en: {
      title: "SimRail Train & Interaction Radar",
      windowLabel: "Window:",
      windowUnit: "min",
      dlcBtn: "DLCs",
      autoOn: "Auto-Refresh: ON ({s}s)",
      autoPause: "Auto-Refresh: PAUSED",
      refreshNow: "Refresh now",
      exitBtn: "Exit",
      loading: "LOADING...",
      live: "LIVE",
      error: "ERROR",
      statServer: "Server",
      statStations: "Active Signal Boxes",
      statPlayers: "Player Trains",
      statBots: "Available AI Trains",
      statFiltered: "{n} hidden by DLC filter",
      statWindow: "Timeframe: Next {m} min",
      myTrainTitle: "My Active Train",
      trainPlaceholder: "Train number (e.g. 41026)",
      trackBtn: "Track train",
      releaseBtn: "Clear / Release",
      noActiveTrain: "No active train selected. Click 'Take train' below or enter your train number above.",
      activeMissing: "Saved train is no longer active on {srv} (destination reached).",
      emptyAheadWarn: "⚠️ No players or dispatchers ahead of you right now. Consider switching to one of the trains below.",
      topTrainsHeader: "Recommended Free Trains (weight: train ×{wt} / station ×{wd})",
      updatedAt: "Updated: {t} (PID {pid})",
      noCandidates: "No free trains with player interactions found within the timeframe.",
      takeTrainBtn: "Take train",
      trainWord: "Train",
      stopBadge: "STOPPED AT PLATFORM (0 km/h)",
      moveBadge: "Moving ({v} km/h)",
      interactionsCount: "{n} interactions",
      dispatchedHeader: "Player Signal Boxes ({n})",
      dispatchedNone: "Player Signal Boxes: None (AI only)",
      inApproxMin: "in ~{m} min",
      inCaMin: "in ~{m} min",
      distAway: "currently {d} km away",
      encountersHeader: "Player Encounters ({n})",
      encountersNone: "Player Trains: No encounters in timeframe",
      kind_opposite: "Oncoming Traffic",
      kind_opposite_gps: "Oncoming (GPS Radar)",
      kind_overtake: "Same Direction (Overtaking)",
      kind_crossing: "Crossing / Station",
      kind_nearby_gps: "Nearby (GPS Radar)",
      loc_between: "between {a} and {b}",
      loc_at: "at/near {a}",
      loc_open_line: "on open line (towards {a})",
      mapDefaultTitle: "Live Map (click a train to highlight encounters)",
      mapFocusTitle: "Live Map • Focus on Train {no} ({pos})",
      legFocus: "Selected / Active Train",
      legOpposite: "Oncoming Player Train",
      legOvertake: "Same Direction / Crossing",
      legOther: "Other Player Train",
      legStation: "Player Signal Box (Dispatcher)",
      popupStation: "Signal Box: {name}",
      popupStationSub: "Dispatched by player",
      popupPlayer: "Player Train {no}",
      popupTarget: "Destination: {t}",
      popupFocus: "Focus: Train {no}",
      popupAt: "At: {pos}",
      prioBalanced: "Focus: Balanced (Equal)",
      prioTrains: "Focus: More Player Trains",
      prioDispatchers: "Focus: More Signal Boxes",
      dlcTitle: "My Owned Rolling Stock DLCs",
      dlcSub: "Check the DLCs you own. Unchecked vehicles will automatically be hidden from free train recommendations.",
      dlcCustomLabel: "Additional vehicle filter (comma-separated, optional):",
      cancel: "Cancel",
      save: "Save",
      shutdownTitle: "SimRail Radar Stopped",
      shutdownSub: "The background process has exited. You can close this tab."
    }
  };

  let currentLang = 'de';
  let currentServer = null;
  let autoRefresh = true;
  let refreshIntervalSec = 30;
  let countdown = 30;
  let lastPayload = null;
  let focusedTrain = null;
  let map = null;
  let layerGroup = null;

  function detectBrowserLang() {
    const nav = (navigator.language || 'en').toLowerCase();
    if (nav.startsWith('pl')) return 'pl';
    if (nav.startsWith('de')) return 'de';
    return 'en';
  }

  function t(key, vars = {}) {
    const dict = I18N[currentLang] || I18N.en;
    let str = dict[key] || I18N.en[key] || key;
    Object.keys(vars).forEach(k => {
      str = str.replace(`{${k}}`, vars[k]);
    });
    return str;
  }

  function setLang(lang, persist = true) {
    if (!I18N[lang]) lang = 'en';
    currentLang = lang;
    ['pl', 'de', 'en'].forEach(l => {
      document.getElementById('lang-' + l).classList.toggle('active', l === lang);
    });
    applyStaticTranslations();
    if (lastPayload) renderAll(lastPayload, true);
    if (persist) {
      fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lang: lang })
      }).catch(() => {});
    }
  }

  function applyStaticTranslations() {
    document.getElementById('ui-title').innerText = t('title');
    document.getElementById('ui-window-label').innerText = t('windowLabel');
    document.getElementById('ui-window-unit').innerText = t('windowUnit');
    document.getElementById('btn-dlc').innerText = t('dlcBtn');
    document.getElementById('btn-refresh').innerText = t('refreshNow');
    document.getElementById('btn-exit').innerText = t('exitBtn');
    document.getElementById('ui-my-train-title').innerText = t('myTrainTitle');
    document.getElementById('active-input').placeholder = t('trainPlaceholder');
    document.getElementById('btn-track').innerText = t('trackBtn');
    document.getElementById('btn-clear-active').innerText = t('releaseBtn');
    document.getElementById('dlc-modal-title').innerText = t('dlcTitle');
    document.getElementById('dlc-modal-sub').innerText = t('dlcSub');
    document.getElementById('dlc-custom-label').innerText = t('dlcCustomLabel');
    document.getElementById('btn-dlc-cancel').innerText = t('cancel');
    document.getElementById('btn-dlc-save').innerText = t('save');
    document.getElementById('opt-prio-balanced').innerText = t('prioBalanced');
    document.getElementById('opt-prio-trains').innerText = t('prioTrains');
    document.getElementById('opt-prio-dispatchers').innerText = t('prioDispatchers');
    document.getElementById('btn-auto').innerText = autoRefresh
      ? t('autoOn', { s: countdown })
      : t('autoPause');

    document.getElementById('map-legend').innerHTML = `
      <span><i class="dot" style="background:#22c55e;"></i>${t('legFocus')}</span>
      <span><i class="dot" style="background:#ef4444;"></i>${t('legOpposite')}</span>
      <span><i class="dot" style="background:#a855f7;"></i>${t('legOvertake')}</span>
      <span><i class="dot" style="background:#3b82f6;"></i>${t('legOther')}</span>
      <span><i class="diamond" style="background:#eab308;"></i>${t('legStation')}</span>
    `;
  }

  if (typeof L !== 'undefined') {
    map = L.map('map').setView([51.6, 19.4], 7);
    L.tileLayer('/tiles/{z}/{y}/{x}', {
      attribution: 'Esri, SimRail API',
      maxZoom: 16
    }).addTo(map);
    layerGroup = L.layerGroup().addTo(map);
  }

  // Pausiert das Polling automatisch, solange der Browser-Tab im Hintergrund ist
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && autoRefresh && countdown <= 3) {
      loadData();
    }
  });

  setInterval(() => {
    if (!autoRefresh || document.hidden) return;
    countdown--;
    if (countdown <= 0) {
      loadData();
    } else {
      document.getElementById('btn-auto').innerText = t('autoOn', { s: countdown });
    }
  }, 1000);

  function toggleAutoRefresh() {
    autoRefresh = !autoRefresh;
    if (autoRefresh) {
      countdown = refreshIntervalSec;
      document.getElementById('btn-auto').innerText = t('autoOn', { s: countdown });
    } else {
      document.getElementById('btn-auto').innerText = t('autoPause');
    }
  }

  function openDlcModal() {
    if (!lastPayload) return;
    const owned = new Set(lastPayload.config.owned_dlcs || []);
    const catalog = lastPayload.config.dlc_catalog || [];
    const container = document.getElementById('dlc-checkboxes');
    container.innerHTML = catalog.map(d => {
      const checked = owned.has(d.id) ? 'checked' : '';
      const label = (d.label && (d.label[currentLang] || d.label.en)) || d.id;
      return `<label class="dlc-item">
        <input type="checkbox" value="${d.id}" ${checked} />
        <span>${label}</span>
      </label>`;
    }).join('');
    document.getElementById('dlc-custom-input').value = (lastPayload.config.custom_excluded || []).join(', ');
    document.getElementById('dlc-modal').classList.add('open');
  }

  function closeDlcModal() {
    document.getElementById('dlc-modal').classList.remove('open');
  }

  async function saveDlcSettings() {
    const checks = document.querySelectorAll('#dlc-checkboxes input[type="checkbox"]');
    const owned = [];
    checks.forEach(ch => { if (ch.checked) owned.push(ch.value); });
    const rawCustom = document.getElementById('dlc-custom-input').value;
    const customList = rawCustom.split(',').map(s => s.trim()).filter(Boolean);

    await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ owned_dlcs: owned, custom_excluded: customList })
    });
    closeDlcModal();
    loadData();
  }

  async function shutdownServer() {
    autoRefresh = false;
    try {
      await fetch('/api/shutdown', { method: 'POST' });
    } catch (e) {}
    document.body.innerHTML = `<div style="padding:40px;text-align:center;color:#e6e9f0;"><h2>${t('shutdownTitle')}</h2><p style="color:#8b95a5;margin-top:8px;">${t('shutdownSub')}</p></div>`;
  }

  function changeServer() {
    currentServer = document.getElementById('server-select').value;
    focusedTrain = null;
    loadData();
  }

  async function changePriority() {
    const prio = document.getElementById('priority-select').value;
    await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ priority: prio })
    });
    loadData();
  }

  async function setActiveTrain(trainNoOverride = null) {
    const val = trainNoOverride || document.getElementById('active-input').value.trim();
    if (!val) return;
    await fetch('/api/active-train', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ server: currentServer, train_no: val })
    });
    document.getElementById('active-input').value = val;
    focusedTrain = null;
    loadData();
  }

  async function clearActiveTrain() {
    await fetch('/api/active-train', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ clear: true })
    });
    document.getElementById('active-input').value = '';
    focusedTrain = null;
    loadData();
  }

  function formatEncounterLocation(e) {
    if (e.loc_mode === 'between') return t('loc_between', { a: e.loc_a, b: e.loc_b });
    if (e.loc_mode === 'at') return t('loc_at', { a: e.loc_a });
    return t('loc_open_line', { a: e.loc_a });
  }

  function renderTrainDetails(tr, isActiveCard) {
    const speedBadge = tr.speed === 0
      ? `<span class="pill pill-stop">${t('stopBadge')}</span>`
      : `<span class="pill pill-move">${t('moveBadge', { v: tr.speed })}</span>`;

    const actionBtn = isActiveCard
      ? ''
      : `<button class="btn-primary" onclick="event.stopPropagation(); setActiveTrain('${tr.train_no}')">${t('takeTrainBtn')}</button>`;

    let fdlHtml = `<div class="detail-group"><div class="detail-label">${t('dispatchedNone')}</div></div>`;
    if (tr.dispatched && tr.dispatched.length > 0) {
      const tags = tr.dispatched.map(d => `<span class="fdl-tag">${d.name} (${t('inApproxMin', { m: d.eta_min })})</span>`).join('');
      fdlHtml = `<div class="detail-group"><div class="detail-label">${t('dispatchedHeader', { n: tr.dispatched.length })}</div><div class="fdl-tags">${tags}</div></div>`;
    }

    let encHtml = `<div class="detail-group"><div class="detail-label">${t('encountersNone')}</div></div>`;
    if (tr.encounters && tr.encounters.length > 0) {
      const items = tr.encounters.map(e => {
        const isOpp = e.kind === 'opposite' || e.kind === 'opposite_gps';
        const oppClass = isOpp ? 'opposite' : '';
        const arrow = isOpp ? '⇄' : '→';
        const typeLabel = t('kind_' + e.kind);
        const locLabel = formatEncounterLocation(e);
        const dist = e.dist_km !== null ? ` • ${t('distAway', { d: e.dist_km })}` : '';
        return `<li class="enc-item ${oppClass}">
          <strong>${arrow} [${typeLabel}] ${t('trainWord')} ${e.train_no}</strong> (${e.vehicle})<br/>
          <span class="enc-meta">${locLabel} • <strong>${t('inCaMin', { m: e.eta_min })}</strong>${dist}</span>
        </li>`;
      }).join('');
      encHtml = `<div class="detail-group"><div class="detail-label">${t('encountersHeader', { n: tr.encounters.length })}</div><ul class="enc-list">${items}</ul></div>`;
    }

    return `
      <div class="card-header">
        <div>
          <span class="train-title">${t('trainWord')} ${tr.train_no}</span>
          <span class="train-veh">| ${tr.name} (${tr.vehicle})</span>
          <div style="margin-top:4px;">${speedBadge} <span class="pill pill-count">${t('interactionsCount', { n: tr.total_interactions })}</span></div>
        </div>
        ${actionBtn}
      </div>
      <div class="route-line">📍 <strong>${tr.current_pos}</strong> &nbsp;➔&nbsp; 🏁 ${tr.target}</div>
      ${fdlHtml}
      ${encHtml}
    `;
  }

  function focusOnTrain(tr) {
    focusedTrain = tr;
    renderAll(lastPayload, false);
  }

  function renderMap(data) {
    if (!map || !layerGroup || !data) return;
    layerGroup.clearLayers();

    const targetTrain = focusedTrain || data.active_train;
    const encounterMap = new Map();
    if (targetTrain && targetTrain.encounters) {
      targetTrain.encounters.forEach(e => encounterMap.set(e.train_no, e));
    }

    // 1. Besetzte Stellwerke
    (data.map.stations || []).forEach(st => {
      if (!st.coords || st.coords[0] === null) return;
      const stIcon = L.divIcon({
        className: '',
        html: '<div style="width:11px;height:11px;background:#eab308;border:2px solid #000;transform:rotate(45deg);box-shadow:0 0 6px rgba(234,179,8,0.85);"></div>',
        iconSize: [11, 11],
        iconAnchor: [5, 5]
      });
      L.marker(st.coords, { icon: stIcon, zIndexOffset: 500 })
        .bindPopup(`<strong>${t('popupStation', { name: st.name })}</strong><br/>${t('popupStationSub')}`)
        .addTo(layerGroup);
    });

    // 2. Andere Spieler-Züge
    (data.map.players || []).forEach(pl => {
      if (!pl.coords || pl.coords[0] === null) return;
      const enc = encounterMap.get(pl.train_no);
      const isOpposite = enc && (enc.kind === 'opposite' || enc.kind === 'opposite_gps');
      const fillColor = enc ? (isOpposite ? '#ef4444' : '#a855f7') : '#3b82f6';
      const statusLabel = enc ? `<br/><em>${t('kind_' + enc.kind)} (${t('inCaMin', { m: enc.eta_min })})</em>` : '';

      L.circleMarker(pl.coords, {
        radius: enc ? 8 : 5,
        fillColor: fillColor,
        color: '#fff',
        weight: enc ? 2 : 1,
        fillOpacity: 0.95
      }).bindPopup(`<strong>${t('popupPlayer', { no: pl.train_no })}</strong><br/>${pl.vehicle}<br/>${t('popupTarget', { t: pl.target })} (${pl.speed} km/h)${statusLabel}`).addTo(layerGroup);
    });

    // 3. Gewählter / Aktiver Zug (nur wenn explizit aktiv oder angeklickt)
    if (targetTrain && targetTrain.coords && targetTrain.coords[0] !== null) {
      L.circleMarker(targetTrain.coords, {
        radius: 10,
        fillColor: '#22c55e',
        color: '#052e16',
        weight: 2,
        fillOpacity: 1
      }).bindPopup(`<strong>${t('popupFocus', { no: targetTrain.train_no })}</strong><br/>${targetTrain.vehicle}<br/>${t('popupAt', { pos: targetTrain.current_pos })} (${targetTrain.speed} km/h)`).addTo(layerGroup);

      document.getElementById('map-title').innerText = t('mapFocusTitle', { no: targetTrain.train_no, pos: targetTrain.current_pos });
    } else {
      document.getElementById('map-title').innerText = t('mapDefaultTitle');
    }
  }

  function renderAll(data, skipFit = false) {
    lastPayload = data;
    refreshIntervalSec = data.config.refresh_sec;
    countdown = refreshIntervalSec;

    const winInput = document.getElementById('window-input');
    if (document.activeElement !== winInput) {
      winInput.value = data.config.window_min;
    }

    const sel = document.getElementById('server-select');
    if (sel.options.length === 0) {
      data.servers.forEach(s => {
        const opt = document.createElement('option');
        opt.value = s.code;
        opt.text = `${s.code.toUpperCase()} - ${s.name}`;
        sel.appendChild(opt);
      });
    }
    sel.value = data.server;
    currentServer = data.server;
    
    sel.value = data.server;
    currentServer = data.server;
    if (data.config.priority) {
      document.getElementById('priority-select').value = data.config.priority;
    }

    document.getElementById('stats-bar').innerHTML = `
      <span>${t('statServer')}: <strong>${data.server.toUpperCase()}</strong></span>
      <span>${t('statStations')}: <strong>${data.stats.stations}</strong></span>
      <span>${t('statPlayers')}: <strong>${data.stats.players}</strong></span>
      <span>${t('statBots')}: <strong>${data.stats.available_bots}</strong> (${t('statFiltered', { n: data.stats.excluded_bots })})</span>
      <span><strong>${t('statWindow', { m: data.config.window_min })}</strong></span>
    `;
    document.getElementById('last-update').innerText = t('updatedAt', { t: data.timestamp, pid: data.pid });

    const activeContainer = document.getElementById('active-train-content');
    const clearBtn = document.getElementById('btn-clear-active');
    if (data.active_train) {
      document.getElementById('active-input').value = data.active_train.train_no;
      clearBtn.style.display = 'inline-block';
      const emptyHint = data.active_train.total_interactions === 0
        ? `<div style="margin-top:10px; color:#fcd34d; font-size:0.85rem;">${t('emptyAheadWarn')}</div>`
        : '';
      activeContainer.innerHTML = `<div class="train-card selected" style="margin-bottom:0;" onclick="focusOnTrain(lastPayload.active_train)">
        ${renderTrainDetails(data.active_train, true)}
        ${emptyHint}
      </div>`;
    } else {
      clearBtn.style.display = 'none';
      const msg = data.active_missing
        ? t('activeMissing', { srv: data.server.toUpperCase() })
        : t('noActiveTrain');
      activeContainer.innerHTML = `<span style="color:var(--muted)">${msg}</span>`;
    }

    const listEl = document.getElementById('candidates-list');
    document.getElementById('candidates-header').innerText =
      t('topTrainsHeader', { wt: data.config.w_train, wd: data.config.w_disp });

    if (!data.candidates || data.candidates.length === 0) {
      listEl.innerHTML = `<div class="train-card">${t('noCandidates')}</div>`;
    } else {
      listEl.innerHTML = data.candidates.map((tr, idx) => {
        const isSel = focusedTrain && focusedTrain.train_no === tr.train_no ? 'selected' : '';
        return `<div class="train-card ${isSel}" onclick="focusOnTrain(lastPayload.candidates[${idx}])">
          ${renderTrainDetails(tr, false)}
        </div>`;
      }).join('');
    }

    renderMap(data);

    const targetTrain = focusedTrain || data.active_train;
    if (map && !skipFit && targetTrain && targetTrain.coords && targetTrain.coords[0] !== null) {
      map.setView(targetTrain.coords, 9);
    }
  }

  async function loadData() {
    const badge = document.getElementById('status-badge');
    badge.innerText = t('loading');
    try {
      const winVal = document.getElementById('window-input').value || 30;
      const params = new URLSearchParams();
      if (currentServer) params.set('server', currentServer);
      params.set('window', winVal);

      const res = await fetch('/api/data?' + params.toString());
      const data = await res.json();
      if (!res.ok || data.error || !data.config) {
        badge.innerText = t('error');
        document.getElementById('stats-bar').innerHTML =
          `<span style="color:#fca5a5;">API Error: ${data.error || 'Invalid response'}</span>`;
        return;
      }

      if (!lastPayload) {
        const savedLang = data.config.lang;
        const initialLang = (savedLang && savedLang !== 'auto') ? savedLang : detectBrowserLang();
        setLang(initialLang, false);
      }

      if (focusedTrain) {
        const updatedFocus = (data.candidates || []).find(c => c.train_no === focusedTrain.train_no)
          || (data.active_train && data.active_train.train_no === focusedTrain.train_no ? data.active_train : null);
        focusedTrain = updatedFocus;
      }
      renderAll(data, lastPayload !== null);
      badge.innerText = t('live');
    } catch (err) {
      badge.innerText = t('error');
      console.error(err);
    }
  }

  setLang(detectBrowserLang(), false);
  loadData();
</script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_bytes(self, content, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        self.wfile.write(content)

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        touch_activity()
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(body)

        elif parsed.path == "/vendor/leaflet.js":
            data = fetch_vendor_asset(
                "leaflet.js",
                [
                    "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js",
                    "https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js",
                    "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.js",
                ],
            )
            if data:
                self._send_bytes(data, "application/javascript; charset=utf-8")
            else:
                self.send_response(502)
                self.end_headers()

        elif parsed.path == "/vendor/leaflet.css":
            data = fetch_vendor_asset(
                "leaflet.css",
                [
                    "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css",
                    "https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css",
                    "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.css",
                ],
            )
            if data:
                self._send_bytes(data, "text/css; charset=utf-8")
            else:
                self.send_response(502)
                self.end_headers()

        elif parsed.path.startswith("/tiles/"):
            parts = parsed.path.strip("/").split("/")
            if len(parts) == 4:
                _, z, y, x = parts
                tile = fetch_map_tile(z, y, x)
                if tile:
                    self._send_bytes(tile, "image/png")
                    return
            self.send_response(404)
            self.end_headers()

        elif parsed.path == "/api/data":
            qs = parse_qs(parsed.query)
            srv = qs.get("server", [None])[0]
            win = qs.get("window", [None])[0]
            try:
                data = fetch_dashboard_payload(
                    requested_server=srv, requested_window=win
                )
                self._send_json(data)
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        touch_activity()
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        body = json.loads(raw)

        if parsed.path == "/api/active-train":
            if body.get("clear"):
                clear_saved_train()
            elif body.get("train_no"):
                srv = body.get("server") or CONFIG["DEFAULT_SERVER"]
                save_active_train(srv, body["train_no"])
            self._send_json({"ok": True})

        elif parsed.path == "/api/settings":
            allowed = {}
            for k in ("lang", "priority", "owned_dlcs", "custom_excluded", "server", "window_min"):
                if k in body:
                    allowed[k] = body[k]
            save_user_settings(allowed)
            self._send_json({"ok": True})

        elif parsed.path == "/api/shutdown":
            self._send_json({"ok": True})
            remove_pid_file()
            threading.Timer(0.15, lambda: os._exit(0)).start()
        else:
            self.send_response(404)
            self.end_headers()


if __name__ == "__main__":
    stop_previous_instance()
    write_pid_file()
    threading.Thread(target=idle_watchdog, daemon=True).start()
    port = CONFIG["PORT"]
    url = f"http://127.0.0.1:{port}/?v={APP_VERSION}"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        server.serve_forever()
    except OSError:
        webbrowser.open(url)
    finally:
        remove_pid_file()