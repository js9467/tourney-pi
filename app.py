from flask import Flask, jsonify, request, send_from_directory, send_file, abort, redirect
from dateutil import parser as date_parser
from datetime import datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo
import json
import os
from bs4 import BeautifulSoup
import requests
import random
import re
from concurrent.futures import ThreadPoolExecutor
import time
import subprocess
from threading import Thread, Lock
import threading
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from email.utils import formataddr
from PIL import Image, ImageOps, ImageFile
import io
from urllib.parse import urljoin, urlparse, urlunparse, parse_qs, urlencode, quote
from collections import defaultdict
import sys
import pwd

def safe_str(s: str) -> str:
    # Ensure no surrogate code points sneak into responses/logs
    return (s or "").encode("utf-8", "replace").decode("utf-8", "replace")

def safe_print(*args, **kwargs):
    # Print without crashing on odd characters
    text = " ".join(safe_str(str(a)) for a in args)
    print(text, **{k:v for k,v in kwargs.items() if k not in ("file",)})


# Allow processing of partially downloaded images
ImageFile.LOAD_TRUNCATED_IMAGES = True

# ------------------------
# Constants / Config
# ------------------------
ALERTS_FILE = 'alerts.json'
NOTIFIED_FILE = 'notified.json'
MASTER_JSON_URL = "https://js9467.github.io/Brtourney/settings.json"

SMTP_USER = "bigrockapp@gmail.com"
SMTP_PASS = "coslxivgfqohjvto"  # Gmail App Password
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587

app = Flask(__name__)
CACHE_FILE = 'cache.json'
SETTINGS_FILE = 'settings.json'
DEMO_DATA_FILE = 'demo_data.json'

BOAT_FOLDER = "static/images/boats"
os.makedirs(BOAT_FOLDER, exist_ok=True)

# Limit size for downloaded boat images (width, height)
IMAGE_MAX_SIZE = (400, 400)

app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 24 * 3600  # cache static files for a day

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0 Safari/537.36",
]

image_locks = {}
# Map of uid -> (boat_name, image_url, base_url) for deferred downloads
IMAGE_SOURCES = {}

# Shared thread pool for background image downloads
IMAGE_DL_EXECUTOR = ThreadPoolExecutor(max_workers=6)
TOURNAMENTS_CACHE = "cache/tournaments.json"

# HTTP session (faster + connection reuse) & suppress SSL warnings for verify=False
SESS = requests.Session()
try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass

# ------------------------
# Utilities
# ------------------------
def safe_json_load(path, default):
    """Read JSON file, return default on missing/empty/corrupt."""
    try:
        if os.path.exists(path) and os.path.getsize(path) > 1:
            with open(path, "r") as f:
                return json.load(f)
    except Exception as e:
        print(f"⚠️ JSON read failed for {path}: {e}")
    return default

def safe_json_dump(path, obj):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except Exception:
        pass
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)

def fetch_html(url, use_scraperapi: bool = False) -> str:
    """Fast, resilient HTML fetch with short timeouts."""
    headers = {
        "User-Agent": random.choice(UA_POOL),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    # First try direct (short timeout)
    try:
        r = SESS.get(url, headers=headers, timeout=12, verify=False)
        if r.status_code == 200 and r.text.strip():
            return r.text
        print(f"⚠️ Direct fetch {r.status_code} for {url}")
    except Exception as e:
        print(f"⚠️ Direct fetch error for {url}: {e}")

    # Optional ScraperAPI
    if use_scraperapi:
        api_key = "e6f354c9c073ceba04c0fe82e4243ebd"
        api = f"https://api.scraperapi.com?api_key={api_key}&keep_headers=true&url={quote(url, safe='')}"
        try:
            r = SESS.get(api, headers=headers, timeout=18)
            if r.status_code == 200 and r.text.strip():
                return r.text
            print(f"⚠️ ScraperAPI failed: HTTP {r.status_code}")
        except Exception as e:
            print(f"❌ Error via ScraperAPI: {e}")

    # Final quick retry direct
    time.sleep(0.5)
    try:
        r = SESS.get(url, headers=headers, timeout=10, verify=False)
        if r.status_code == 200 and r.text.strip():
            return r.text
        print(f"⚠️ Final retry got {r.status_code} for {url}")
    except Exception as e:
        print(f"⚠️ Final retry error for {url}: {e}")
    return ""

def load_alerts():
    return safe_json_load(ALERTS_FILE, [])

def save_alerts(alerts):
    safe_json_dump(ALERTS_FILE, alerts)

def load_notified_events():
    arr = safe_json_load(NOTIFIED_FILE, [])
    return set(arr)

def save_notified_events(notified):
    safe_json_dump(NOTIFIED_FILE, list(notified))

def get_cache_path(tournament, filename):
    folder = os.path.join("cache", normalize_boat_name(tournament))
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, filename)

def load_cache():
    return safe_json_load(CACHE_FILE, {})

def save_cache(cache):
    safe_json_dump(CACHE_FILE, cache)

def load_settings():
    return safe_json_load(SETTINGS_FILE, {})

def load_demo_data(tournament):
    data = safe_json_load(DEMO_DATA_FILE, {})
    return data.get(tournament, {'events': [], 'leaderboard': []})

def get_data_source():
    s = load_settings()
    return (s.get("data_source") or s.get("mode") or "live").lower()

def is_cache_fresh(cache, key, max_age_minutes):
    try:
        last_scraped = cache.get(key, {}).get("last_scraped")
        if not last_scraped:
            return False
        last_time = datetime.fromisoformat(last_scraped)
        return (datetime.now() - last_time) < timedelta(minutes=max_age_minutes)
    except Exception:
        return False

def get_current_tournament():
    settings = load_settings()
    return settings.get('tournament', 'Big Rock')

def get_tournament_logo() -> str | None:
    """Return the logo URL/path for the current tournament."""
    tournament = get_current_tournament()
    try:
        settings = SESS.get(MASTER_JSON_URL, timeout=12).json()
        key = next((k for k in settings if k.lower() == tournament.lower()), None)
        if key:
            return settings[key].get("logo")
    except Exception as e:
        print(f"⚠️ Failed to fetch tournament logo: {e}")
    return None

def normalize_boat_name(name):
    """Normalize boat names for comparison/storage.

    Converts to ASCII, lowercases, and replaces any non-alphanumeric
    characters with underscores. This allows following boats whose names
    contain spaces, apostrophes, or other special characters.
    """
    if not name:
        return "unknown"
    import unicodedata, re
    # Convert to ASCII and lowercase
    ascii_name = (
        unicodedata.normalize("NFKD", name)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    # Replace any remaining non-alphanumeric characters with underscores
    return re.sub(r"[^a-z0-9]+", "_", ascii_name).strip("_")

# ------------------------
# Image handling
# ------------------------
def _resolve_boat_image_fs(uid: str) -> str | None:
    """Return filesystem path to the best available image, if any."""
    base = os.path.join(BOAT_FOLDER, uid)
    for ext in (".webp", ".jpg", ".jpeg", ".png"):
        candidate = base + ext
        if os.path.exists(candidate) and os.path.getsize(candidate) > 0:
            return candidate
    return None

@app.route("/boat-image/<uid>")
def boat_image(uid):
    """Serve a boat's image or a default placeholder, with logging."""
    try:
        fs_path = _resolve_boat_image_fs(uid)
        if fs_path:
            try:
                with Image.open(fs_path) as img:
                    if img.getexif().get(274, 1) != 1:
                        img = ImageOps.exif_transpose(img)
                        if img.mode in ("RGBA", "LA", "P"):
                            img = img.convert("RGB")
                        img.save(fs_path)
            except Exception as e:
                print(f"⚠️ Auto-orient failed for {uid}: {e}")
            return send_file(fs_path, max_age=24 * 3600)
        # If we know a source URL for this uid, fetch it asynchronously
        info = IMAGE_SOURCES.get(uid)
        if info:
            boat_name, img_url, base_url = info
            IMAGE_DL_EXECUTOR.submit(cache_boat_image, boat_name, img_url, base_url)
        logo = get_tournament_logo()
        if logo:
            if logo.startswith("http://") or logo.startswith("https://"):
                return redirect(logo)
            logo_path = logo.lstrip("/")
            if os.path.exists(logo_path):
                return send_file(logo_path, max_age=24 * 3600)
        default_path = os.path.join("static", "images", "bigrock.png")
        if os.path.exists(default_path):
            return send_file(default_path, max_age=24 * 3600)
        return abort(404)
    except Exception as e:
        print(f"❌  /boat-image error for {uid}: {e}")
        return abort(500)

def _get_best_img_src(img_tag) -> str | None:
    if not img_tag:
        return None
    for attr in ("src", "data-src", "data-lazy-src", "data-original", "data-image"):
        val = img_tag.get(attr)
        if val and str(val).strip():
            return val.strip()
    return None

def cache_boat_image(boat_name, image_url, base_url=None):
    """Download and save the boat image (webp + modest resize)."""
    os.makedirs(BOAT_FOLDER, exist_ok=True)
    uid = normalize_boat_name(boat_name)
    if base_url:
        image_url = urljoin(base_url, image_url)

    file_path = os.path.join(BOAT_FOLDER, f"{uid}.webp")
    lock = image_locks.setdefault(file_path, Lock())

    with lock:
        existing = _resolve_boat_image_fs(uid)
        if existing:
            return f"/boat-image/{uid}"

        headers = {
            "User-Agent": random.choice(UA_POOL),
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Referer": base_url or "",
        }
        for attempt in range(2):
            try:
                r = SESS.get(image_url, headers=headers, timeout=12)
                if r.status_code == 200 and r.content:
                    img_bytes = io.BytesIO(r.content)
                    try:
                        with Image.open(img_bytes) as img:
                            img = ImageOps.exif_transpose(img)
                            img.thumbnail(IMAGE_MAX_SIZE)
                            if img.mode in ("RGBA", "LA", "P"):
                                img = img.convert("RGB")
                            img.save(file_path, "WEBP", quality=80)
                        print(f"✅ Downloaded image for {boat_name}: {file_path}")
                    except Exception as e:
                        with open(file_path, "wb") as f:
                            f.write(r.content)
                        print(f"⚠️ Saved unoptimized image for {boat_name}: {e}")
                    return f"/boat-image/{uid}"
                print(f"⚠️ Image HTTP {r.status_code} for {boat_name} → {image_url}")
            except Exception as e:
                print(f"⚠️ Error downloading image for {boat_name} (try {attempt+1}/2): {e}")
            time.sleep(0.4)
        return f"/boat-image/{uid}"

# ------------------------
# Tournament dates utilities
# ------------------------
def _nice_range_label(start_dt: datetime, end_dt: datetime) -> str:
    if start_dt.year != end_dt.year:
        return f"{start_dt.strftime('%b %-d, %Y')} – {end_dt.strftime('%b %-d, %Y')}"
    if start_dt.month == end_dt.month:
        return f"{start_dt.strftime('%b %-d')}–{end_dt.strftime('%-d, %Y')}"
    return f"{start_dt.strftime('%b %-d')} – {end_dt.strftime('%b %-d, %Y')}"

def _parse_date_range_any(text: str, default_year: int | None = None):
    text = (text or "").strip()
    if not text:
        return None, None
    cleaned = text.replace("–", "-").replace("—", "-").replace(" to ", "-").replace(" – ", "-")
    m = re.search(r"([A-Za-z]{3,9})\s+(\d{1,2})\s*-\s*([A-Za-z]{3,9})?\s*(\d{1,2})(?:,\s*(\d{4}))?", cleaned)
    if not m:
        try:
            dt = date_parser.parse(cleaned, default=datetime(default_year or datetime.now().year, 1, 1))
            return dt, dt
        except:
            return None, None
    m1, d1, m2, d2, y = m.groups()
    year = int(y) if y else (default_year or datetime.now().year)
    try:
        start = date_parser.parse(f"{m1} {d1}, {year}")
        if m2:
            end = date_parser.parse(f"{m2} {d2}, {year}")
        else:
            end = date_parser.parse(f"{m1} {d2}, {year}")
        if end < start:
            end = start
        return start, end
    except:
        return None, None

def _scrape_dates_from_html(html: str):
    try:
        soup = BeautifulSoup(html, "html.parser")
        text = " ".join(t.get_text(" ", strip=True) for t in soup.find_all(["h1","h2","h3","p","li","div","span"]))
        candidates = []
        for pat in [
            r"(?:Tournament Dates?:?\s*)?([A-Za-z]{3,9}\s+\d{1,2}\s*[-–]\s*[A-Za-z]{0,9}\s*\d{1,2}(?:,\s*\d{4})?)",
            r"([A-Za-z]{3,9}\s+\d{1,2},?\s*(?:-\s*[A-Za-z]{0,9}\s*\d{1,2})?,?\s*\d{4})",
            r"([A-Za-z]{3,9}\s+\d{1,2}\s*[-–]\s*\d{1,2},?\s*\d{4})",
        ]:
            for m in re.finditer(pat, text):
                frag = m.group(1)
                if 5 <= len(frag) <= 40:
                    candidates.append(frag)
        for c in candidates:
            s, e = _parse_date_range_any(c)
            if s and e:
                return s, e
        return None, None
    except:
        return None, None

def build_tournaments_index(force: bool = False):
    os.makedirs("cache", exist_ok=True)
    cached = safe_json_load(TOURNAMENTS_CACHE, {}) if not force else {}
    try:
        master = SESS.get(MASTER_JSON_URL, timeout=15).json()
    except Exception as e:
        print(f"❌ Failed to fetch MASTER_JSON_URL: {e}")
        return cached
    entries = {}
    if isinstance(master, dict):
        entries = master
    elif isinstance(master, list):
        for obj in master:
            if isinstance(obj, dict):
                name = obj.get("name") or obj.get("tournament")
                if name:
                    entries[name] = obj
    else:
        print(f"⚠️ Unexpected master JSON type: {type(master)}")
        return cached
    out = {}
    for name, vals in entries.items():
        if not isinstance(vals, dict):
            print(f"⚠️ Skipping '{name}' (not a dict)")
            continue
        if not force and name in cached and cached[name].get("start") and cached[name].get("end"):
            out[name] = cached[name]
            continue
        pages = [vals.get("leaderboard"), vals.get("events"), vals.get("participants"), vals.get("activities")]
        pages = [p for p in pages if isinstance(p, str) and p.strip()]
        if not pages:
            out[name] = {"start": None, "end": None, "label": ""}
            continue
        s_dt = e_dt = None
        for url in pages:
            html = fetch_html(url)
            if not html:
                continue
            s_dt, e_dt = _scrape_dates_from_html(html)
            if s_dt and e_dt:
                break
        if s_dt and e_dt:
            label = _nice_range_label(s_dt, e_dt)
            out[name] = {"start": s_dt.strftime("%Y-%m-%d"), "end": e_dt.strftime("%Y-%m-%d"), "label": label}
        else:
            out[name] = {"start": None, "end": None, "label": ""}
    safe_json_dump(TOURNAMENTS_CACHE, out)
    print(f"✅ Tournaments index saved: {len(out)} entries")
    return out

# ------------------------
# Demo event injection
# ------------------------
def inject_hooked_up_events(events, tournament=None):
    demo_events = []
    inserted_keys = set()
    name_summary = re.compile(r"^[A-Z][a-z]+\s+[A-Z][a-z]+\s+(released|boated|weighed)", re.IGNORECASE)
    events = [e for e in events if not name_summary.match(e.get("details", ""))]

    events.sort(key=lambda e: date_parser.parse(e["timestamp"]))
    for event in events:
        boat = event.get("boat", "Unknown")
        uid = event.get("uid", "unknown")
        etype = event.get("event", "").lower()
        details = event.get("details", "").lower()
        is_resolution = ("boated" in etype or "released" in etype or
                         "pulled hook" in details or "wrong species" in details)
        if not is_resolution:
            continue
        try:
            resolution_ts = date_parser.parse(event["timestamp"])
            event_date = resolution_ts.date()
            start_time = datetime.combine(event_date, dt_time(9, 0))
            delta = timedelta(minutes=random.randint(5, 90))
            hook_ts = max(start_time, resolution_ts - delta)
            key = f"{uid}_{resolution_ts.isoformat()}"
            if key in inserted_keys:
                continue
            demo_events.append({
                "timestamp": hook_ts.isoformat(),
                "event": "Hooked Up",
                "boat": boat,
                "uid": uid,
                "details": "Hooked up!",
                "hookup_id": key
            })
            # Attach the same hookup_id to the resolution event so we can
            # pair them later when filtering unresolved hooks
            event["hookup_id"] = key
            inserted_keys.add(key)
        except Exception as e:
            print(f"⚠️ Demo injection failed for {boat}: {e}")
    all_events = sorted(events + demo_events, key=lambda e: date_parser.parse(e["timestamp"]))
    print(f"📦 Returning {len(all_events)} events (with {len(demo_events)} hooked up injections)")
    return all_events

def build_demo_cache(tournament: str) -> int:
    print(f"📦 [DEMO] Building demo cache for {tournament}...")
    try:
        events = scrape_events(force=True, tournament=tournament)
        if not events:
            events_file = get_cache_path(tournament, "events.json")
            events = safe_json_load(events_file, [])
            if events:
                print(f"🟡 Using cached {len(events)} live events for demo injection")
        injected = inject_hooked_up_events(events, tournament)
        leaderboard = scrape_leaderboard(tournament, force=True) or []
        demo_data = safe_json_load(DEMO_DATA_FILE, {})
        demo_data[tournament] = {"events": injected, "leaderboard": leaderboard}
        safe_json_dump(DEMO_DATA_FILE, demo_data)
        print(f"✅ [DEMO] Saved demo_data.json for {tournament} with {len(injected)} events")
        return len(injected)
    except Exception as e:
        print(f"❌ [DEMO] Failed to build demo cache: {e}")
        return 0

# ------------------------
# Scrapers
# ------------------------
def run_in_thread(target, name):
    def wrapper():
        try:
            print(f"🧵 Starting {name} in thread...")
            target()
            print(f"✅ Finished {name}.")
        except Exception as e:
            print(f"❌ Error in {name} thread: {e}")
    Thread(target=wrapper, daemon=True).start()

def scrape_participants(force: bool = False):
    cache = load_cache()
    tournament = get_current_tournament()
    participants_file = get_cache_path(tournament, "participants.json")
    cache_key = f"{tournament}_participants"

    if not force and is_cache_fresh(cache, cache_key, 1440):
        return safe_json_load(participants_file, [])

    try:
        settings = SESS.get(MASTER_JSON_URL, timeout=12).json()
        matching_key = next((k for k in settings if k.lower() == tournament.lower()), None)
        if not matching_key:
            raise Exception(f"Tournament '{tournament}' not found in settings.json")
        participants_url = settings[matching_key].get("participants")
        if not participants_url:
            raise Exception(f"No participants URL found for {matching_key}")

        print(f"📡 Scraping participants from: {participants_url}")
        html = fetch_html(participants_url)
        if not html:
            safe_json_dump(participants_file, [])
            print("⚠️ Error scraping participants: no HTML — wrote empty participants.json")
            return []

        soup = BeautifulSoup(html, 'html.parser')

        updated_participants = {}
        download_tasks = []
        seen_boats = set()

        for article in soup.select("article.post.format-image, article"):
            name_tag = article.select_one("h2.post-title, h2, h3")
            type_tag = article.select_one("ul.post-meta li")  # best-effort
            img_tag  = article.select_one("img")
            if not name_tag:
                continue

            boat_name = name_tag.get_text(strip=True)
            if not boat_name or boat_name.lower() in seen_boats or ',' in boat_name:
                continue
            seen_boats.add(boat_name.lower())

            uid = normalize_boat_name(boat_name)
            boat_type = type_tag.get_text(strip=True) if type_tag else ""

            img_src = _get_best_img_src(img_tag) if img_tag else None
            if img_src:
                img_src = urljoin(participants_url, img_src)
                IMAGE_SOURCES[uid] = (boat_name, img_src, participants_url)

            updated_participants[uid] = {
                "uid": uid,
                "boat": boat_name,
                "type": boat_type,
                "image_path": f"/boat-image/{uid}",
            }

            if img_src:
                download_tasks.append((uid, boat_name, img_src, participants_url))

        safe_json_dump(participants_file, list(updated_participants.values()))
        print(f"💾 participants.json written with {len(updated_participants)} entries")

        if download_tasks:
            print(f"📸 Scheduling download of {len(download_tasks)} boat images...")
            for uid, bname, url, base in download_tasks:
                IMAGE_DL_EXECUTOR.submit(cache_boat_image, bname, url, base)

        cache[cache_key] = {"last_scraped": datetime.now().isoformat()}
        save_cache(cache)
        return list(updated_participants.values())
    except Exception as e:
        print(f"⚠️ Error scraping participants: {e}")
        if not os.path.exists(participants_file):
            safe_json_dump(participants_file, [])
        return []

def _unique(seq):
    seen = set()
    out = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out

def _same_path_root(a, b):
    try:
        pa, pb = urlparse(a), urlparse(b)
        return (pa.scheme, pa.netloc, pa.path.split('/')[1:3]) == (pb.scheme, pb.netloc, pb.path.split('/')[1:3])
    except:
        return True

def discover_event_page_urls(events_url: str, soup: BeautifulSoup) -> list[str]:
    """
    Find all pagination URLs for the Events feed.
    Keep it tight: explicit links + a few probes.
    """
    base = events_url
    urls = [base]

    # explicit pagination links
    for sel in ["ul.pagination a", "nav.pagination a", ".pagination a",
                "a.page-numbers", "ul.page-numbers a", "a[rel='next']", "a[rel='prev']"]:
        for a in soup.select(sel):
            href = a.get("href")
            if not href:
                continue
            u = urljoin(base, href.strip())
            if _same_path_root(base, u):
                urls.append(u)

    urls = _unique(urls)

    if len(urls) > 1:
        def page_key(u):
            m = re.search(r"/page/(\d+)/?$", u) or re.search(r"[?&](?:page|paged|p)=(\d+)", u)
            return int(m.group(1)) if m else 1
        urls = [base] + sorted([u for u in urls if u != base], key=page_key)
        return urls[:8]  # cap

    # fallback: probe only first few pages to avoid slow drags
    probed = [base]
    for i in range(2, 6):  # max 5 pages total
        candidate = urljoin(base.rstrip('/') + '/', f"page/{i}/")
        probed.append(candidate)

    # alt fallback: ?page=N
    parsed = urlparse(base)
    qs = parse_qs(parsed.query)
    for i in range(2, 6):
        new_qs = qs.copy()
        new_qs["page"] = [str(i)]
        new_query = urlencode(new_qs, doseq=True)
        alt = urlunparse(parsed._replace(query=new_query))
        probed.append(alt)

    return _unique(probed)

def scrape_events(force: bool = False, tournament: str | None = None):
    cache = load_cache()
    tournament = tournament or get_current_tournament()
    events_file = get_cache_path(tournament, "events.json")
    cache_key = f"events_{tournament}"

    if not force and is_cache_fresh(cache, cache_key, 2):
        return safe_json_load(events_file, [])

    try:
        remote = SESS.get(MASTER_JSON_URL, timeout=12).json()
        key = next((k for k in remote if normalize_boat_name(k) == normalize_boat_name(tournament)), None)
        if not key:
            raise Exception(f"Tournament '{tournament}' not found in remote settings.json")
        events_url = remote[key].get("events")
        if not events_url:
            raise Exception(f"No events URL found for {tournament}")

        print(f"📡 Scraping events (with pagination) from: {events_url}")
        first_html = fetch_html(events_url)
        if not first_html:
            safe_json_dump(events_file, [])
            cache[cache_key] = {"last_scraped": datetime.now().isoformat()}
            save_cache(cache)
            print("❌ Failed to fetch events HTML — wrote empty events.json")
            return []

        first_soup = BeautifulSoup(first_html, 'html.parser')
        page_urls = discover_event_page_urls(events_url, first_soup)

        participants_file = get_cache_path(tournament, "participants.json")
        participants = {p["uid"]: p for p in safe_json_load(participants_file, []) if p.get("uid")}

        all_events, seen = [], set()

        def parse_events_from_soup(soup):
            found = 0
            for article in soup.select("article.m-b-20, article.entry, div.activity, li.event, div.feed-item"):
                time_tag = article.select_one("p.pull-right, time, .time")
                name_tag = article.select_one("h4.montserrat, h4, h3")
                desc_tag = article.select_one("p > strong, strong, .desc, .details")
                if not time_tag or not name_tag or not desc_tag:
                    continue
                raw = time_tag.get_text(strip=True).replace("@", "").strip()
                try:
                    ts = date_parser.parse(raw).replace(year=datetime.now().year).isoformat()
                except:
                    continue
                boat = name_tag.get_text(strip=True)
                desc = desc_tag.get_text(strip=True)
                uid = normalize_boat_name(boat)

                if uid in participants:
                    boat = participants[uid]["boat"]

                low = desc.lower()
                if "released" in low:
                    event_type = "Released"
                elif "boated" in low:
                    event_type = "Boated"
                elif "pulled hook" in low:
                    event_type = "Pulled Hook"
                elif "wrong species" in low:
                    event_type = "Wrong Species"
                elif "hooked up" in low:
                    event_type = "Hooked Up"
                else:
                    event_type = "Other"

                dkey = f"{uid}_{event_type}_{ts}"
                if dkey in seen:
                    continue
                seen.add(dkey)

                all_events.append({
                    "timestamp": ts,
                    "event": event_type,
                    "boat": boat,
                    "uid": uid,
                    "details": desc
                })
                found += 1
            return found

        # parse first page
        parsed_count = parse_events_from_soup(first_soup)
        consecutive_empty = 0 if parsed_count else 1
        max_consecutive_empty = 2

        # iterate additional pages
        for url in page_urls[1:]:
            html = fetch_html(url)
            if not html:
                consecutive_empty += 1
                if consecutive_empty >= max_consecutive_empty:
                    break
                continue
            soup = BeautifulSoup(html, 'html.parser')
            found = parse_events_from_soup(soup)
            if found == 0:
                consecutive_empty += 1
                if consecutive_empty >= max_consecutive_empty:
                    break
            else:
                consecutive_empty = 0

        # Sort newest first
        all_events.sort(key=lambda e: e["timestamp"], reverse=True)

        safe_json_dump(events_file, all_events)
        cache[cache_key] = {"last_scraped": datetime.now().isoformat()}
        save_cache(cache)
        print(f"✅ Scraped {len(all_events)} events across {len(page_urls)} page(s) for {tournament}")
        return all_events
    except Exception as e:
        print(f"❌ Error in scrape_events: {e}")
        if not os.path.exists(events_file):
            safe_json_dump(events_file, [])
        cache[cache_key] = {"last_scraped": datetime.now().isoformat()}
        save_cache(cache)
        return []

# ---------- Leaderboard helpers ----------
KNOWN_BUILDERS = {
    "viking","jarrett","jarrett bay","bayliss","hatteras","post","ocean","bertram",
    "carolina","spencer","garlington","rampage","custom","contender","freeman","maverick"
}

def split_boat_and_type(name: str, text_after: str):
    """
    Heuristic: keep 'name' as boat; extract size/builder/model into 'type'.
    Falls back gracefully if we can't detect anything reliable.
    """
    name = (name or "").strip()
    extra = (text_after or "").strip()
    if not extra:
        return name, None

    extra_clean = re.sub(r"\s+", " ", extra)
    # If extra contains a length or a known builder, treat as type.
    contains_len = bool(re.search(r"\b\d{2,3}\s*(?:ft|feet|')\b", extra_clean.lower()))
    contains_builder = any(b in extra_clean.lower() for b in KNOWN_BUILDERS)
    if contains_len or contains_builder or len(extra_clean) <= 40:
        return name, extra_clean

    # If 'name' looks like it includes type info (rare), try to split  "Boat – 68' Jarrett Bay"
    m = re.search(r"(.*?)[\-\–—]\s*(.*)$", name)
    if m:
        return m.group(1).strip(), (m.group(2) + (" " + extra_clean if extra_clean else "")).strip()

    return name, None

def parse_points_number(points_text: str) -> float:
    if not points_text:
        return 0.0
    txt = points_text.lower()
    # handle "1,200 pts", "1200", "500 lb"
    nums = re.findall(r"[\d\.,]+", txt)
    if not nums:
        return 0.0
    try:
        return float(nums[0].replace(",", ""))
    except:
        return 0.0

def scrape_leaderboard(tournament=None, force: bool = False):
    cache = load_cache()
    tournament = tournament or get_current_tournament()
    lb_file = get_cache_path(tournament, "leaderboard.json")
    cache_key = f"leaderboard_{tournament}"

    if not force and is_cache_fresh(cache, cache_key, 2):
        return safe_json_load(lb_file, [])

    try:
        remote = SESS.get(MASTER_JSON_URL, timeout=15).json()
        key = next((k for k in remote if k.lower() == tournament.lower()), None)
        if not key:
            print(f"❌ Tournament '{tournament}' not found in master JSON.")
            safe_json_dump(lb_file, [])
            return []
        leaderboard_url = remote[key].get("leaderboard")
        if not leaderboard_url:
            print(f"❌ No leaderboard URL for '{tournament}'.")
            safe_json_dump(lb_file, [])
            return []

        print(f"📡 Scraping leaderboard for {tournament} → {leaderboard_url}")
        html = fetch_html(leaderboard_url)
        if not html:
            safe_json_dump(lb_file, [])
            print("⚠️ No leaderboard HTML — wrote empty leaderboard.json")
            return []

        soup = BeautifulSoup(html, "html.parser")
        leaderboard = []

        categories = [a.get_text(strip=True) for a in soup.select("ul.dropdown-menu li a.leaderboard-nav, a[data-toggle='tab']")]
        categories = [c for c in categories if c]
        categories = list(dict.fromkeys(categories))  # dedupe, keep order
        if not categories:
            print("⚠️ No categories found; attempting single-table scrape")

        def collect_rows_from_container(container, category_label):
            for row in container.select("tr.montserrat, tr"):
                cols = row.find_all("td")
                if len(cols) < 2:
                    continue
                rank = cols[0].get_text(strip=True)
                boat_block = cols[1]
                points = cols[-1].get_text(strip=True)
                h4 = boat_block.find("h4") or boat_block.find("strong") or boat_block.find("b")
                name = h4.get_text(strip=True) if h4 else boat_block.get_text(" ", strip=True)
                text_after = boat_block.get_text(" ", strip=True).replace(name, "").strip()

                angler, boat, btype = None, name, None
                # If points looks like weight and block doesn't look like a boat, treat as angler category
                if "lb" in points.lower() and not any(b in text_after.lower() for b in KNOWN_BUILDERS):
                    angler, boat, btype = name, None, None
                else:
                    boat, btype = split_boat_and_type(name, text_after)

                uid = normalize_boat_name(boat or angler or f"rank_{rank}")
                leaderboard.append({
                    "rank_raw": rank,
                    "category": category_label or "Overall",
                    "angler": angler,
                    "boat": boat,
                    "type": btype,
                    "points": points,
                    "points_num": parse_points_number(points),
                    "uid": uid,
                    "image_path": f"/boat-image/{uid}",
                })

        if categories:
            for category in categories:
                tab_link = soup.find("a", string=lambda x: x and x.strip() == category)
                tab_id = tab_link.get("href") if tab_link else None
                tab = soup.select_one(tab_id) if tab_id else None
                if tab:
                    collect_rows_from_container(tab, category)
        else:
            table = soup.find("table")
            if table:
                collect_rows_from_container(table, "Overall")

        # Normalize ranks: same points = same position (per category)
        by_cat = defaultdict(list)
        for row in leaderboard:
            by_cat[row["category"]].append(row)

        normalized = []
        for cat, rows in by_cat.items():
            rows.sort(key=lambda r: (-r["points_num"], r["boat"] or r["angler"] or ""))
            last_points = None
            pos = 0
            for idx, r in enumerate(rows, start=1):
                if r["points_num"] != last_points:
                    pos = idx
                    last_points = r["points_num"]
                r["rank"] = str(pos)  # overwrite normalized rank
                normalized.append(r)

        safe_json_dump(lb_file, normalized)
        cache[cache_key] = {"last_scraped": datetime.now().isoformat()}
        save_cache(cache)
        print(f"✅ Scraped {len(normalized)} leaderboard entries for {tournament}")
        return normalized
    except Exception as e:
        print(f"❌ Error in scrape_leaderboard: {e}")
        if not os.path.exists(lb_file):
            safe_json_dump(lb_file, [])
        cache[cache_key] = {"last_scraped": datetime.now().isoformat()}
        save_cache(cache)
        return []
# ========= Auto audio routing (BT <-> HDMI), robust =========
import pwd, select
from subprocess import CalledProcessError

AUDIO_USER = os.environ.get("AUDIO_USER", "pi")   # desktop user (Chromium owner)
_AUDIO_UID = pwd.getpwnam(AUDIO_USER).pw_uid

def _audio_env():
    env = os.environ.copy()
    env["XDG_RUNTIME_DIR"] = f"/run/user/{_AUDIO_UID}"
    env["PULSE_RUNTIME_PATH"] = f"/run/user/{_AUDIO_UID}/pulse"
    env["PULSE_SERVER"] = f"unix:/run/user/{_AUDIO_UID}/pulse/native"
    return env

def _sudo_prefix():
    # If running as root (e.g. systemd), execute audio tools as the desktop
    # user and ensure the Pulse/PipeWire env vars are preserved.  ``sudo``
    # resets most environment variables, so we explicitly inject them via the
    # ``env`` command to guarantee pactl/wpctl can talk to the user's daemon.
    if os.geteuid() == 0:
        env = _audio_env()
        return [
            "sudo", "-u", AUDIO_USER,
            "env",
            f"XDG_RUNTIME_DIR={env['XDG_RUNTIME_DIR']}",
            f"PULSE_RUNTIME_PATH={env['PULSE_RUNTIME_PATH']}",
            f"PULSE_SERVER={env['PULSE_SERVER']}"
        ]
    return []

def _run_raw(cmd, check=True, timeout=None):
    cp = subprocess.run(cmd, text=True, capture_output=True, env=_audio_env(), timeout=timeout)
    if check and cp.returncode != 0:
        raise CalledProcessError(cp.returncode, cmd, cp.stdout + cp.stderr)
    return cp.stdout

def _run_pulse(cmd, check=True, timeout=None):
    # Always hit the user's Pulse/PipeWire
    full = _sudo_prefix() + cmd
    return _run_raw(full, check=check, timeout=timeout)

def _pactl(*args, check=True, timeout=None):
    return _run_pulse(["pactl", *args], check=check, timeout=timeout)

def _wpctl(*args, check=True, timeout=None):
    return _run_pulse(["wpctl", *args], check=check, timeout=timeout)

def _list_sinks():
    out = _pactl("list", "short", "sinks", check=False)
    sinks = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            sinks.append({"id": parts[0], "name": parts[1], "raw": line})
    # Fallback via wpctl if pactl returns nothing
    if not sinks:
        s = _wpctl("status", check=False)
        block = []
        rec = False
        for ln in s.splitlines():
            if ln.strip().startswith("Sinks:"):
                rec = True
                continue
            if rec:
                if not ln.strip():
                    break
                # lines like "  41. bluez_output.F8_DF_...  [vol: ...]"
                bits = ln.strip().split()
                if len(bits) >= 2 and "." in bits[0]:
                    name = bits[1]
                    sinks.append({"id": bits[0].rstrip("."), "name": name, "raw": ln})
    return sinks

def _list_inputs():
    out = _pactl("list", "short", "sink-inputs", check=False)
    ids = []
    for line in out.splitlines():
        cols = line.split()
        if cols:
            ids.append(cols[0])
    return ids

def _get_default_sink():
    name = _pactl("get-default-sink", check=False).strip()
    return name or None

def _set_default_sink(name):
    if not name:
        return
    _pactl("set-default-sink", name, check=False)

def _move_all_inputs(target):
    for sid in _list_inputs():
        _pactl("move-sink-input", sid, target, check=False)

def _pick_bt_sink(sinks=None):
    sinks = sinks or _list_sinks()
    for s in sinks:
        n = s["name"].lower()
        if "bluez_output" in n or "a2dp" in n:
            return s["name"]
    return None

def _pick_hdmi_sink(sinks=None):
    sinks = sinks or _list_sinks()
    for s in sinks:
        if "hdmi" in s["name"].lower():
            return s["name"]
    for s in sinks:
        if "bluez" not in s["name"].lower():
            return s["name"]
    return None

def _ensure_bt_profile_a2dp():
    """
    Force the bluez card (if present) into a2dp-sink profile.
    """
    out = _pactl("list", "cards", check=False)
    card_name = None
    for ln in out.splitlines():
        t = ln.strip()
        if t.startswith("Name:") and "bluez_card." in t:
            card_name = t.split("Name:",1)[1].strip()
        if card_name and t.startswith("Profiles:"):
            break
    if card_name:
        _pactl("set-card-profile", card_name, "a2dp-sink", check=False)

# ---------- Minimal audio retarget helpers ----------
def _pactl_user(*args):
    # Run pactl in the 'pi' desktop audio context
    return subprocess.check_output(
        ['sudo', '-u', 'pi', 'env', 'XDG_RUNTIME_DIR=/run/user/1000', 'pactl', *args],
        text=True, encoding='utf-8', errors='replace'
    )

def _get_sink_by(pattern: str) -> str | None:
    try:
        out = _pactl_user('list', 'short', 'sinks')
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and pattern.lower() in parts[1].lower():
                return parts[1]
    except Exception as e:
        safe_print(f"_get_sink_by({pattern}) error: {e}")
    return None

def _move_chromium_inputs(target_sink: str) -> int:
    """Move ONLY Chromium/Electron/WebKit sink-inputs to target_sink. Returns count moved."""
    moved = 0
    try:
        detail = _pactl_user('list', 'sink-inputs')
        # Split per input block
        for block in detail.split('Sink Input #'):
            block = block.strip()
            if not block or not block.splitlines()[0].strip().isdigit():
                continue
            idx = block.splitlines()[0].strip()

            # app detection: chromium, electron, webkit, browser
            low = block.lower()
            is_browser = ('chromium' in low or
                          'application.name = "chromium"' in block.lower() or
                          'application.name = "Chromium"' in block or
                          'electron' in low or
                          'webkit' in low or
                          'browser' in low)

            if is_browser:
                try:
                    _pactl_user('move-sink-input', idx, target_sink)
                    moved += 1
                except subprocess.CalledProcessError as me:
                    safe_print(f"move-sink-input {idx}->{target_sink} failed: {me}")

    except Exception as e:
        safe_print(f"_move_chromium_inputs error: {e}")
    return moved

# ---------- Minimal test route ----------
@app.route('/audio/retarget', methods=['POST'])
def audio_retarget():
    """
    Prefer BT sink if available; else HDMI; else keep default.
    Moves Chromium/Electron/WebKit sink-inputs to chosen sink.
    Optional JSON body: {"prefer":"bt"|"hdmi"}  (default "bt")
    """
    try:
        prefer = (request.json or {}).get('prefer', 'bt')
    except Exception:
        prefer = 'bt'

    bt = _get_sink_by('bluez_output')
    hdmi = _get_sink_by('hdmi')

    target = None
    if prefer == 'bt' and bt:
        target = bt
    elif prefer == 'hdmi' and hdmi:
        target = hdmi

    if not target:
        # Fallback to current default
        try:
            target = _pactl_user('get-default-sink').strip()
        except Exception:
            target = None

    if not target:
        return jsonify({"status": "error", "message": "No sink available"}), 500

    # Make it default (harmless even if already default)
    try:
        _pactl_user('set-default-sink', target)
    except Exception as e:
        safe_print(f"set-default-sink {target} failed: {e}")

    moved = _move_chromium_inputs(target)

    return jsonify({"status": "ok", "target": target, "moved": moved})

def _reconcile_audio_route(verbose=True):
    """
    If a BT (bluez) sink exists -> set default to BT and move streams.
    Otherwise -> default to HDMI (or first non-bluez) and move streams.
    """
    try:
        sinks = _list_sinks()
        bt = _pick_bt_sink(sinks)
        hdmi = _pick_hdmi_sink(sinks)
        current = _get_default_sink()

        if bt:
            if current != bt:
                _set_default_sink(bt)
                time.sleep(0.3)
            _move_all_inputs(bt)
            if verbose: safe_print(f"🔊 Routed to Bluetooth sink: {bt}")
            return {"routed_to": "bluetooth", "sink": bt}
        else:
            if hdmi and current != hdmi:
                _set_default_sink(hdmi)
                time.sleep(0.3)
            if hdmi:
                _move_all_inputs(hdmi)
                if verbose: safe_print(f"🔊 Routed to HDMI sink: {hdmi}")
                return {"routed_to": "hdmi", "sink": hdmi}
            if verbose: safe_print("⚠️ No HDMI or BT sinks found; leaving as-is.")
            return {"routed_to": "unknown", "sink": current}
    except Exception as e:
        safe_print(f"⚠️ reconcile error: {e}")
        return {"error": str(e)}

def _audio_router_monitor():
    """
    Background watcher:
      - Subscribes to pactl events (new/remove sink/input, card changes)
      - Periodic reconcile as a safety net
    """
    safe_print("🎧 Audio router monitor started.")
    proc = None
    try:
        proc = subprocess.Popen(
            _sudo_prefix() + ["pactl", "subscribe"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=_audio_env()
        )
    except Exception as e:
        safe_print(f"❌ pactl subscribe failed: {e}")

    last_tick = 0
    while True:
        try:
            line = None
            if proc and proc.stdout:
                r, _, _ = select.select([proc.stdout], [], [], 0.5)
                if r:
                    line = proc.stdout.readline().strip()

            now = time.time()
            if line:
                low = line.lower()
                if any(k in low for k in ("on sink", "on sink-input", "on card", "new", "remove", "change")):
                    _reconcile_audio_route(verbose=False)

            if now - last_tick > 3.0:
                _reconcile_audio_route(verbose=False)
                last_tick = now

        except Exception as e:
            safe_print(f"⚠️ audio monitor loop error: {e}")
            time.sleep(1)

_audio_monitor_lock = Lock()
_audio_monitor_thread = None

def start_audio_router_monitor():
    """Start the audio router monitor once per process."""
    global _audio_monitor_thread
    with _audio_monitor_lock:
        if _audio_monitor_thread and _audio_monitor_thread.is_alive():
            return
        if any(t.name == "audio_router_monitor" for t in threading.enumerate()):
            return
        _audio_monitor_thread = Thread(
            target=_audio_router_monitor,
            daemon=True,
            name="audio_router_monitor",
        )
        _audio_monitor_thread.start()
# Start monitor at import time
start_audio_router_monitor()
# ========= end auto audio routing =========




# ------------------------
# Routes: pages & static
# ------------------------
@app.route('/')
def homepage():
    return send_from_directory('templates', 'index.html')

@app.route('/offline')
def offline_page():
    return send_from_directory('templates', 'offline.html')

@app.route('/participants')
def participants_page():
    return send_from_directory('static', 'participants.html')

@app.route('/leaderboard')
def leaderboard_page():
    return send_from_directory('static', 'leaderboard.html')

@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory('static', filename, cache_timeout=24 * 3600)

@app.route('/healthz')
def healthz():
    return "ok", 200

# ------------------------
# Routes: scraping & data APIs
# ------------------------
@app.route('/scrape/participants')
def scrape_participants_route():
    limit = int(request.args.get('limit', 100))
    offset = int(request.args.get('offset', 0))
    participants = scrape_participants(force=True)
    sliced = participants[offset:offset + limit]
    return jsonify({"count": len(participants), "participants": sliced, "status": "ok"})

@app.route("/scrape/leaderboard")
def scrape_leaderboard_route():
    force = request.args.get("force") == "1"
    tournament = get_current_tournament()
    data = scrape_leaderboard(tournament, force=force)
    return jsonify({"status": "ok" if data else "error", "leaderboard": data})

@app.route("/participants_data")
def participants_data():
    print("📥 /participants_data requested")
    tournament = get_current_tournament()
    participants_file = get_cache_path(tournament, "participants.json")
    participants = safe_json_load(participants_file, [])
    if not participants:
        print(f"⚠️ No participants.json for {tournament} — scraping immediately...")
        participants = scrape_participants(force=True)
    for p in participants:
        p["uid"] = p.get("uid") or normalize_boat_name(p.get("boat","unknown"))
        p["image_path"] = f"/boat-image/{p['uid']}"
    participants.sort(key=lambda p: (p.get("boat") or "").lower())
    return jsonify({"status": "ok", "participants": participants, "count": len(participants)})

@app.route("/scrape/events")
def scrape_events_route():
    settings = load_settings()
    tournament = get_current_tournament()

    if settings.get("data_source") == "demo":
        data = load_demo_data(tournament)
        if not data.get("events"):
            print("⚠️ demo_data.json empty — building now …")
            build_demo_cache(tournament)
            data = load_demo_data(tournament)

        all_events = data.get("events", [])

        eastern = ZoneInfo("America/New_York")
        now = datetime.now(eastern)
        today = now.date()

        filtered = []
        for e in all_events:
            try:
                original_ts = date_parser.parse(e["timestamp"])
                if original_ts.tzinfo is None:
                    original_ts = original_ts.replace(tzinfo=eastern)
                else:
                    original_ts = original_ts.astimezone(eastern)
                ts = datetime.combine(today, original_ts.timetz())
            except Exception:
                continue

            if ts <= now:
                adjusted = dict(e)
                adjusted["timestamp"] = ts.isoformat()
                filtered.append(adjusted)

        filtered.sort(key=lambda e: e["timestamp"], reverse=True)

        return jsonify({"status": "ok", "count": len(filtered), "events": filtered[:100]})

    try:
        events = scrape_events(force=True, tournament=tournament)
        events.sort(key=lambda e: e["timestamp"], reverse=True)
        return jsonify({"status": "ok", "count": len(events), "events": events[:100]})
    except Exception as e:
        print(f"❌ Error in /scrape/events: {e}")
        return jsonify({"status": "error", "message": str(e)})

@app.route("/scrape/all")
def scrape_all():
    tournament = get_current_tournament()
    print(f"🔁 Starting full scrape for tournament: {tournament}")
    participants = scrape_participants(force=True)
    events = scrape_events(force=True, tournament=tournament)
    leaderboard = scrape_leaderboard(tournament, force=True)
    return jsonify({
        "status": "ok",
        "tournament": tournament,
        "events": len(events),
        "participants": len(participants),
        "leaderboard": len(leaderboard),
        "message": "Scraped all data and cached it."
    })

@app.route("/status")
def get_status():
    try:
        cache = load_cache()
        tournament = get_current_tournament()
        data_source = load_settings().get("data_source", "live")
        status = {
            "mode": data_source,
            "tournament": tournament,
            "participants_last_scraped": None,
            "events_last_scraped": None,
            "leaderboard_last_scraped": None,
            "participants_cache_fresh": False,
            "events_cache_fresh": False,
            "leaderboard_cache_fresh": False,
        }
        part_key = f"{tournament}_participants"
        event_key = f"events_{tournament}"
        lb_key = f"leaderboard_{tournament}"
        if part_key in cache:
            ts = cache[part_key].get("last_scraped")
            status["participants_last_scraped"] = ts
            status["participants_cache_fresh"] = is_cache_fresh(cache, part_key, 1440)
        if event_key in cache:
            ts = cache[event_key].get("last_scraped")
            status["events_last_scraped"] = ts
            status["events_cache_fresh"] = is_cache_fresh(cache, event_key, 2)
        if lb_key in cache:
            ts = cache[lb_key].get("last_scraped")
            status["leaderboard_last_scraped"] = ts
            status["leaderboard_cache_fresh"] = is_cache_fresh(cache, lb_key, 2)
        return jsonify(status)
    except Exception as e:
        print(f"❌ Error in /status: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# ------------------------
# Alerts / Email
# ------------------------
def send_boat_email_alert(event):
    boat = event.get('boat', 'Unknown')
    action = event.get('event', 'Activity')
    timestamp = event.get('timestamp', datetime.now().isoformat())
    uid = event.get('uid', 'unknown')
    details = event.get('details', 'No additional details provided')

    subject = f"{boat} {action}"
    if details and details.lower() != 'hooked up!':
        subject += f" — {details}"
    subject += f" at {timestamp}"

    base_path = f"static/images/boats/{uid}"
    image_path = None
    for ext in [".jpg", ".jpeg", ".png", ".webp"]:
        candidate = base_path + ext
        if os.path.exists(candidate):
            image_path = candidate
            break
    if not image_path and os.path.exists("static/images/palmer_lou.jpg"):
        print(f"⚠️ No image for {boat}, using fallback Palmer Lou")
        image_path = "static/images/palmer_lou.jpg"

    recipients = load_alerts()
    if not recipients:
        print("No recipients for email alert.")
        return 0

    success = 0
    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            for recipient in recipients:
                try:
                    msg = MIMEMultipart("related")
                    msg['From'] = formataddr(("BigRock Alerts", SMTP_USER))
                    msg['To'] = recipient
                    msg['Subject'] = subject

                    msg_alt = MIMEMultipart("alternative")
                    msg.attach(msg_alt)

                    text_body = f"""🚤 {boat} {action}!
Time: {timestamp}
Details: {details}
BigRock Live Alert
"""
                    msg_alt.attach(MIMEText(text_body, "plain"))

                    html_body = f"""
                    <html><body>
                        <p>🚤 <b>{boat}</b> {action}!<br>
                        Time: {timestamp}<br>
                        Details: {details}</p>
                        <img src="cid:boat_image" style="max-width: 600px; height: auto;">
                    </body></html>
                    """
                    msg_alt.attach(MIMEText(html_body, "html"))

                    if image_path and os.path.exists(image_path):
                        try:
                            with Image.open(image_path) as img:
                                img = ImageOps.exif_transpose(img)
                                img.thumbnail((600, 600))
                                img_bytes = io.BytesIO()
                                if img.mode in ("RGBA", "LA", "P"):
                                    img = img.convert("RGB")
                                img.save(img_bytes, format="JPEG", quality=70)
                                img_bytes.seek(0)
                                image = MIMEImage(img_bytes.read(), name=f"{uid}.jpg")
                                image.add_header("Content-ID", "<boat_image>")
                                image.add_header("Content-Disposition", "inline", filename=f"{uid}.jpg")
                                msg.attach(image)
                        except Exception as e:
                            print(f"⚠️ Could not resize/attach image: {e}")

                    server.sendmail(SMTP_USER, [recipient], msg.as_string())
                    print(f"✅ Email alert sent to {recipient} for {boat} {action}")
                    success += 1
                except Exception as e:
                    print(f"❌ Failed to send alert to {recipient}: {e}")
    except Exception as e:
        print(f"❌ SMTP batch failed: {e}")
    return success

emailed_events = set()

def load_emailed_events():
    return set(safe_json_load(NOTIFIED_FILE, []))

def save_emailed_events():
    safe_json_dump(NOTIFIED_FILE, list(emailed_events))

def get_followed_boats():
    settings = load_settings()
    # Older configs stored "followed boats" under a misspelled key. Prefer the
    # correct key but fall back to the legacy one so users don't lose their
    # selections.
    boats = settings.get("followed_boats") or settings.get("followed_boots", [])
    return [normalize_boat_name(b) for b in boats]

def _build_pactl_env(user: str) -> dict | None:
    """Return env vars so pactl talks to user's Pulse/PipeWire session."""
    try:
        uid = pwd.getpwnam(user).pw_uid
        env = os.environ.copy()
        env["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"
        return env
    except Exception as e:
        safe_print(f"pactl env setup failed for {user}: {e}")
        return None


def _find_bt_sink_name(mac: str, env: dict | None = None) -> str | None:
    """Return Pulse/PipeWire sink name for a BT device MAC (underscored)."""
    try:
        sinks = subprocess.check_output(
            ['pactl', 'list', 'short', 'sinks'],
            text=True, encoding='utf-8', errors='replace', env=env
        )
        mac_id = mac.replace(':', '_').lower()
        for line in sinks.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                name = parts[1]
                low = name.lower()
                if 'bluez' in low or mac_id in low:
                    return name
    except Exception as e:
        safe_print(f"find_bt_sink error: {e}")
    return None


def _route_all_audio_to_sink(sink_name: str, env: dict | None = None):
    """Move every current sink-input to the target sink, then set it default."""
    try:
        # Make it default
        subprocess.check_output(
            ['pactl', 'set-default-sink', sink_name],
            text=True, encoding='utf-8', errors='replace', env=env
        )
        # Move existing streams (Chromium, system sounds, etc.)
        inputs = subprocess.check_output(
            ['pactl', 'list', 'short', 'sink-inputs'],
            text=True, encoding='utf-8', errors='replace', env=env
        )
        for line in inputs.splitlines():
            cols = line.split()
            if not cols:
                continue
            input_id = cols[0]
            try:
                subprocess.check_output(
                    ['pactl', 'move-sink-input', input_id, sink_name],
                    text=True, encoding='utf-8', errors='replace', env=env
                )
            except Exception as e:
                safe_print(f"move-sink-input {input_id} -> {sink_name} failed: {e}")
    except Exception as e:
        safe_print(f"route_all_audio_to_sink error: {e}")

def should_email(event):
    etype = event.get("event", "").lower()
    uid = event.get("uid", "")
    if "boated" in etype:
        return True
    followed_boats = [normalize_boat_name(b) for b in load_settings().get("followed_boats", [])]
    return uid in followed_boats

def process_new_event(event):
    global emailed_events
    uid = f"{event.get('timestamp')}_{event.get('uid')}_{event.get('event')}"
    if uid in emailed_events:
        return
    emailed_events.add(uid)
    save_emailed_events()
    if should_email(event):
        try:
            send_boat_email_alert(event)
            print(f"📧 Email sent for {event['boat']} - {event['event']}")
        except Exception as e:
            print(f"❌ Email failed for {event['boat']}: {e}")

def background_event_emailer():
    """Continuously watches new events and sends emails (respects demo/live)."""
    global emailed_events
    emailed_events = load_emailed_events()
    print(f"📡 Email watcher started. Loaded {len(emailed_events)} previous notifications.")
    try:
        # Preload last 50 as already-emailed to avoid flood
        tournament = get_current_tournament()
        events_file = get_cache_path(tournament, "events.json")
        events = safe_json_load(events_file, [])
        events.sort(key=lambda e: e["timestamp"], reverse=True)
        for e in events[:50]:
            key = f"{e.get('timestamp')}_{e.get('uid')}_{e.get('event')}"
            emailed_events.add(key)
        save_emailed_events()
        print(f"⏩ Preloaded {min(50, len(events))} events as already emailed")
    except Exception as e:
        print(f"⚠️ Failed to preload events: {e}")

    while True:
        try:
            settings = load_settings()
            tournament = get_current_tournament()
            if settings.get("data_source") == "demo":
                data = load_demo_data(tournament)
                events = data.get("events", [])
                now = datetime.now().time()
                events = [e for e in events if date_parser.parse(e["timestamp"]).time() <= now]
            else:
                events_file = get_cache_path(tournament, "events.json")  # recompute each loop
                events = safe_json_load(events_file, [])
            events.sort(key=lambda e: e["timestamp"], reverse=True)
            for e in events[:50]:
                process_new_event(e)
        except Exception as e:
            print(f"⚠️ Email watcher error: {e}")
        time.sleep(30)

# ------------------------
# Routes: alerts
# ------------------------
@app.route('/alerts/list', methods=['GET'])
def list_alerts():
    return jsonify(load_alerts())

@app.route('/alerts/subscribe', methods=['POST'])
def subscribe_alerts():
    data = request.get_json()
    new_emails = data.get('sms_emails', [])
    alerts = load_alerts()
    for email in new_emails:
        if email and email not in alerts:
            alerts.append(email)
    save_alerts(alerts)
    return jsonify({"status": "subscribed", "count": len(alerts)})

@app.route('/alerts/test', methods=['GET'])
def test_alerts():
    boat_name = "Palmer Lou"
    action = "Hooked Up"
    action_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    image_path = "static/images/palmer_lou.jpg"
    recipients = load_alerts()
    if not recipients:
        return jsonify({"status": "no_subscribers"}), 404
    success = 0
    for recipient in recipients:
        try:
            msg = MIMEMultipart("related")
            msg['From'] = formataddr(("BigRock Alerts", SMTP_USER))
            msg['To'] = recipient
            msg['Subject'] = f"{boat_name} {action} at {action_time}"
            msg_alt = MIMEMultipart("alternative")
            msg.attach(msg_alt)
            text_body = f"🚤 {boat_name} {action}!\nTime: {action_time}\n\nBigRock Live Alert"
            msg_alt.attach(MIMEText(text_body, "plain"))
            html_body = f"""
            <html>
            <body>
                <p>🚤 <b>{boat_name}</b> {action}!<br>
                Time: {action_time}</p>
                <img src="cid:boat_image" style="max-width: 600px; height: auto;">
            </body>
            </html>
            """
            msg_alt.attach(MIMEText(html_body, "html"))
            if os.path.exists(image_path):
                try:
                    with Image.open(image_path) as img:
                        img = ImageOps.exif_transpose(img)
                        img.thumbnail((600, 600))
                        img_bytes = io.BytesIO()
                        if img.mode in ("RGBA", "LA", "P"):
                            img = img.convert("RGB")
                        img.save(img_bytes, format="JPEG", quality=70)
                        img_bytes.seek(0)
                        image = MIMEImage(img_bytes.read(), name=os.path.basename(image_path))
                        image.add_header("Content-ID", "<boat_image>")
                        image.add_header("Content-Disposition", "inline", filename=os.path.basename(image_path))
                        msg.attach(image)
                except Exception as e:
                    print(f"⚠️ Could not resize/attach image: {e}")
            else:
                print(f"❌ Image not found at {image_path}")
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
                server.starttls()
                server.login(SMTP_USER, SMTP_PASS)
                server.sendmail(SMTP_USER, [recipient], msg.as_string())
            print(f"✅ Test email sent to {recipient} with Palmer Lou image inline")
            success += 1
        except Exception as e:
            print(f"❌ Failed to send to {recipient}: {e}")
    return jsonify({"status": "sent", "success_count": success})

# ------------------------
# Settings & UI pages
# ------------------------
@app.route('/api/settings', methods=['GET', 'POST'])
def api_settings():
    if request.method == 'POST':
        settings_data = request.get_json()
        if not settings_data:
            return jsonify({'status': 'error', 'message': 'Invalid JSON'}), 400

        old_settings = load_settings()
        old_tournament = old_settings.get("tournament")
        old_mode = old_settings.get("data_source")
        new_tournament = settings_data.get("tournament")
        new_mode = settings_data.get("data_source")

        # Keep mode/data_source in sync
        if new_mode:
            settings_data["data_source"] = new_mode
            settings_data["mode"] = new_mode

        # Ensure sound fields exist
        settings_data.setdefault("followed_sound", old_settings.get("followed_sound", "fishing reel"))
        settings_data.setdefault("boated_sound",   old_settings.get("boated_sound",   "fishing reel"))

        # Save alerts and settings
        save_alerts(settings_data.get("sms_emails", []))
        safe_json_dump(SETTINGS_FILE, settings_data)

        # Triggers
        if new_mode == "live" and (new_tournament != old_tournament or old_mode != "live"):
            print(f"🔄 Tournament changed or mode to live: {old_tournament} → {new_tournament}")
            run_in_thread(lambda: scrape_participants(force=True), "participants")
            run_in_thread(lambda: scrape_events(force=True, tournament=new_tournament or get_current_tournament()), "events")
            run_in_thread(lambda: scrape_leaderboard(new_tournament or get_current_tournament(), force=True), "leaderboard")
        if new_mode == "demo":
            tournament_to_build = new_tournament or old_tournament or get_current_tournament()
            run_in_thread(lambda: build_demo_cache(tournament_to_build), "demo_cache")

        return jsonify({'status': 'success'})

    settings = load_settings()
    settings.setdefault("followed_sound", "Fishing Reel")
    settings.setdefault("boated_sound", "Fishing Reel")
    settings["sms_emails"] = load_alerts()
    return jsonify(settings)

@app.route('/settings-page/')
def settings_page():
    return send_from_directory('static', 'settings.html')

@app.route("/generate_demo")
def generate_demo():
    try:
        tournament = get_current_tournament()
        count = build_demo_cache(tournament)
        return jsonify({"status": "ok", "events": count})
    except Exception as e:
        print(f"❌ Error generating demo data: {e}")
        return jsonify({"status": "error", "message": str(e)})

@app.route("/api/leaderboard")
def api_leaderboard():
    tournament = get_current_tournament()
    lb_file = get_cache_path(tournament, "leaderboard.json")
    leaderboard = safe_json_load(lb_file, [])
    cache = load_cache()
    lb_key = f"leaderboard_{tournament}"
    cache_valid = bool(leaderboard) and is_cache_fresh(cache, lb_key, 2)
    if not cache_valid:
        print("⚠️ Leaderboard cache empty/stale — scraping fresh")
        leaderboard = scrape_leaderboard(tournament, force=True)

    # Ensure image endpoint + uid set
    for row in leaderboard or []:
        uid = row.get("uid") or normalize_boat_name(row.get("boat", "") or row.get("angler", ""))
        row["uid"] = uid
        row["image_path"] = f"/boat-image/{uid}"

    return jsonify({"status": "ok" if leaderboard else "error", "leaderboard": leaderboard})

@app.route("/hooked")
def get_hooked_up_events():
    settings = load_settings()
    tournament = get_current_tournament()
    data_source = settings.get("data_source", "live").lower()
    eastern = ZoneInfo("America/New_York")
    now = datetime.now(eastern)

    if data_source == "demo":
        data = load_demo_data(tournament)
        events = []
        for e in data.get("events", []):
            try:
                original_ts = date_parser.parse(e["timestamp"])
                if original_ts.tzinfo is None:
                    original_ts = original_ts.replace(tzinfo=eastern)
                else:
                    original_ts = original_ts.astimezone(eastern)
                event_dt = datetime.combine(now.date(), original_ts.timetz())
            except Exception:
                continue
            if event_dt <= now:
                adjusted = dict(e)
                adjusted["timestamp"] = event_dt.isoformat()
                events.append(adjusted)

        # unresolved only (use hookup_id resolution pairing)
        resolved_ids = set()
        for e in events:
            if e["event"] in ["Released", "Boated"] or \
               "pulled hook" in e.get("details", "").lower() or \
               "wrong species" in e.get("details", "").lower():
                key = e.get("hookup_id")
                if key:
                    resolved_ids.add(key)
        hooked_feed = []
        for e in events:
            if e["event"] != "Hooked Up":
                continue
            key = e.get("hookup_id")
            if not key or key not in resolved_ids:
                hooked_feed.append(e)
    else:
        events_file = get_cache_path(tournament, "events.json")
        events = safe_json_load(events_file, [])
        events.sort(key=lambda e: date_parser.parse(e["timestamp"]))
        active_hooks = {}
        for e in events:
            uid = e.get("uid")
            etype = e.get("event", "").lower()
            if etype == "hooked up":
                active_hooks.setdefault(uid, []).append(e)
            elif etype in ["boated", "released"] or \
                 "pulled hook" in e.get("details", "").lower() or \
                 "wrong species" in e.get("details", "").lower():
                if uid in active_hooks and active_hooks[uid]:
                    active_hooks[uid].pop(0)
        hooked_feed = []
        for boat_hooks in active_hooks.values():
            hooked_feed.extend(boat_hooks)

    hooked_feed.sort(key=lambda e: date_parser.parse(e["timestamp"]), reverse=True)
    return jsonify({"status": "ok", "count": len(hooked_feed), "events": hooked_feed[:50]})

@app.route("/api/tournaments", methods=["GET"])
def api_tournaments():
    try:
        if os.path.exists(TOURNAMENTS_CACHE):
            data = safe_json_load(TOURNAMENTS_CACHE, {})
        else:
            data = build_tournaments_index(force=False)
    except Exception as e:
        print(f"⚠️ /api/tournaments failed reading cache: {e}")
        data = build_tournaments_index(force=False)
    return jsonify({"status": "ok", "tournaments": data or {}})

@app.route("/scrape/tournament_dates", methods=["POST", "GET"])
def scrape_tournament_dates():
    data = build_tournaments_index(force=True)
    return jsonify({"status":"ok", "count": len(data), "tournaments": data})

# ------------------------
# Bluetooth / Wi-Fi / Keyboard / Sounds / Version / Release summary
# ------------------------

@app.route('/bluetooth/status')
def bluetooth_status():
    try:
        show = subprocess.check_output(['bluetoothctl', 'show'],
                                       text=True, encoding='utf-8', errors='replace')
        powered = 'Powered: yes' in show
        discovering = 'Discovering: yes' in show
        adapter_addr = None
        adapter_name = None
        for ln in show.splitlines():
            s = ln.strip()
            if s.startswith('Controller '):
                try: adapter_addr = s.split()[1]
                except: pass
            elif s.startswith('Name:'):
                adapter_name = s.split('Name:',1)[1].strip()

        connected = []
        try:
            devs = subprocess.check_output(['bluetoothctl', 'devices', 'Connected'],
                                           text=True, encoding='utf-8', errors='replace')
            for ln in devs.splitlines():
                s = ln.strip()
                if s.startswith('Device '):
                    parts = s.split(' ', 2)
                    if len(parts) >= 3:
                        connected.append({"mac": parts[1], "name": safe_str(parts[2])})
        except Exception:
            pass

        # Also try to infer active A2DP sink from PulseAudio/PipeWire
        active_sink = None
        try:
            sinks = subprocess.check_output(['pactl', 'list', 'short', 'sinks'],
                                            text=True, encoding='utf-8', errors='replace')
            # bluez sinks usually contain "bluez" or device MAC with underscores
            for ln in sinks.splitlines():
                cols = ln.split()
                if len(cols) >= 2 and ('bluez' in cols[1].lower()):
                    active_sink = cols[1]
                    break
        except Exception:
            pass

        return jsonify({
            "enabled": powered,
            "connected": bool(connected),
            "discovering": discovering,
            "adapter": {"address": adapter_addr, "name": safe_str(adapter_name) if adapter_name else None},
            "devices": connected,
            "active_sink": active_sink
        })
    except Exception as e:
        return jsonify({"enabled": False, "connected": False, "devices": [], "error": safe_str(str(e))}), 500



@app.route('/bluetooth/scan')
def bluetooth_scan():
    try:
        safe_print("🔍 Starting Bluetooth scan (12s)…")

        subprocess.check_output(
            ['bluetoothctl', '--timeout', '12', 'scan', 'on'],
            text=True, encoding='utf-8', errors='replace', stderr=subprocess.STDOUT
        )
        # Ensure off
        try:
            subprocess.check_output(['bluetoothctl', 'scan', 'off'],
                                    text=True, encoding='utf-8', errors='replace')
        except Exception:
            pass

        devices_raw = subprocess.check_output(
            ['bluetoothctl', 'devices'],
            text=True, encoding='utf-8', errors='replace'
        )

        discovered = {}
        for line in devices_raw.splitlines():
            ls = line.strip()
            if ls.startswith('Device '):
                parts = ls.split(' ', 2)
                if len(parts) >= 3:
                    mac = parts[1]
                    name = safe_str(parts[2])
                    discovered[mac] = {"mac": mac, "name": name}

        # Keep only audio-capable (A2DP/Headset/Handsfree/AVRCP)
        AUDIO_UUID_SUBSTRS = (
            '0000110b',  # Audio Sink (A2DP)
            '0000110e',  # A/V Remote Control (AVRCP)
            '0000111e',  # Handsfree
            '0000111f',  # Handsfree Audio Gateway
            '0000110a',  # Audio Source
            'Audio Sink', 'Headset', 'Handsfree', 'AVRCP', 'A2DP'
        )

        results = []
        for mac, base in discovered.items():
            try:
                info = subprocess.check_output(
                    ['bluetoothctl', 'info', mac],
                    text=True, encoding='utf-8', errors='replace'
                )
                paired    = 'Paired: yes' in info
                connected = 'Connected: yes' in info

                uuids = []
                for ln in info.splitlines():
                    t = ln.strip()
                    if t.startswith('UUID:'):
                        uuids.append(safe_str(t[5:].strip()))

                # Filter: keep if any audio UUID keyword present
                blob = info.lower()
                is_audio = any(k.lower() in blob for k in AUDIO_UUID_SUBSTRS) or any(k.lower() in ' '.join(uuids).lower() for k in AUDIO_UUID_SUBSTRS)
                if not is_audio:
                    continue

                base.update({
                    "paired": paired,
                    "connected": connected,
                    "uuids": uuids,
                })
                results.append(base)
            except Exception as e_info:
                safe_print(f"info({mac}) failed: {e_info}")

        # Sort: connected first, then paired, then by name
        results.sort(key=lambda d: (not d.get("connected", False),
                                    not d.get("paired", False),
                                    (d.get("name") or d['mac']).lower()))

        safe_print(f"Bluetooth scan returning {len(results)} audio device(s).")
        return jsonify({"devices": results})
    except Exception as e:
        safe_print(f"Bluetooth scan failed: {e}")
        return jsonify({"devices": [], "error": safe_str(str(e))}), 500



@app.route('/bluetooth/connect', methods=['POST'])
def bluetooth_connect():
    data = request.get_json() or {}
    mac = data.get('mac')
    if not mac:
        return jsonify({"status": "error", "message": "Missing 'mac'"}), 400

    try:
        # Connect (pair if needed)
        rc1 = subprocess.run(['bluetoothctl', 'connect', mac], text=True, capture_output=True)
        if rc1.returncode != 0:
            subprocess.run(['bluetoothctl', 'pair', mac], text=True, capture_output=True, check=False)
            rc2 = subprocess.run(['bluetoothctl', 'connect', mac], text=True, capture_output=True)
            if rc2.returncode != 0:
                return jsonify({"status": "error", "message": (rc2.stdout + rc2.stderr).strip()}), 500

        # Give bluez time to publish sinks, then ensure A2DP
        time.sleep(1.5)
        _ensure_bt_profile_a2dp()
        time.sleep(0.5)

        routing = _reconcile_audio_route(verbose=True)

        info = subprocess.check_output(['bluetoothctl', 'info', mac], text=True, errors='replace')
        connected = 'Connected: yes' in info
        paired = 'Paired: yes' in info
        name_line = next((l for l in info.splitlines() if l.strip().startswith('Name:')), None)
        name = name_line.split('Name:', 1)[1].strip() if name_line else mac

        return jsonify({
            "status": "ok" if connected else "error",
            "connected": connected, "paired": paired,
            "device": {"mac": mac, "name": name},
            "routing": routing
        }), 200 if connected else 500

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
@app.route('/audio/route/reconcile', methods=['POST','GET'])
def audio_route_reconcile():
    return jsonify(_reconcile_audio_route(verbose=True))

@app.route('/audio/diag')
def audio_diag():
    try:
        sinks = _list_sinks()
        default = _get_default_sink()
        inputs = _list_inputs()
        return jsonify({"default": default, "sinks": sinks, "inputs": inputs})
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@app.route('/bluetooth/disconnect', methods=['POST'])
def bluetooth_disconnect():
    data = request.get_json() or {}
    mac = data.get('mac')
    if not mac:
        return jsonify({"status": "error", "message": "Missing 'mac'"}), 400
    try:
        subprocess.check_output(
            ['bluetoothctl', 'disconnect', mac],
            text=True,
            encoding='utf-8',
            errors='replace',
            stderr=subprocess.STDOUT,
        )
        try:
            sinks = subprocess.check_output(
                ['pactl', 'list', 'short', 'sinks'],
                text=True,
                encoding='utf-8',
                errors='replace'
            )
            mac_id = mac.replace(':', '_').lower()
            fallback = None
            for line in sinks.splitlines():
                parts = line.split()
                if len(parts) >= 2 and mac_id not in parts[1].lower():
                    fallback = parts[1]
                    break
            if fallback:
                subprocess.check_output(
                    ['pactl', 'set-default-sink', fallback],
                    text=True,
                    encoding='utf-8',
                    errors='replace'
                )
        except Exception:
            pass
        return jsonify({"status": "ok"})
    except subprocess.CalledProcessError as e:
        return jsonify({"status": "error", "message": e.output.strip()}), 500

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/wifi/scan')
def wifi_scan():
    try:
        scan_result = subprocess.check_output(['nmcli', '-t', '-f', 'SSID,SIGNAL,IN-USE', 'dev', 'wifi'], text=True)
        seen = {}
        connected_ssid = None
        for line in scan_result.strip().split('\n'):
            parts = line.strip().split(':')
            if len(parts) >= 3:
                ssid, signal, in_use = parts
                if not ssid.strip():
                    continue
                try:
                    signal = int(signal)
                except ValueError:
                    continue
                is_connected = in_use.strip() == '*'
                if ssid not in seen or is_connected or signal > seen[ssid]['signal']:
                    seen[ssid] = {'ssid': ssid, 'signal': signal, 'connected': is_connected}
                if is_connected:
                    connected_ssid = ssid
        networks = list(seen.values())
        return jsonify({'networks': networks, 'connected': connected_ssid})
    except Exception as e:
        print(f"❌ Wi-Fi scan error: {e}")
        return jsonify({'networks': [], 'connected': None})

@app.route('/wifi/connect', methods=['POST'])
def wifi_connect():
    data = request.get_json()
    ssid = data.get('ssid')
    password = data.get('password', '')
    if not ssid:
        return jsonify({'status': 'error', 'message': 'Missing SSID'}), 400
    try:
        print(f"🔌 Attempting connection to: {ssid}")
        cmd = ['sudo', 'nmcli', 'dev', 'wifi', 'connect', ssid]
        if password:
            cmd += ['password', password]
        result = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True)
        print(f"✅ Connected: {result}")
        return jsonify({'status': 'ok', 'message': result})
    except subprocess.CalledProcessError as e:
        print(f"❌ nmcli error: {e.output}")
        if "Secrets were required" in e.output:
            return jsonify({'status': 'error', 'message': 'Password required for new network', 'code': 'password_required'}), 400
        return jsonify({'status': 'error', 'message': e.output.strip()}), 500

@app.route('/wifi/disconnect', methods=['POST'])
def wifi_disconnect():
    try:
        result = subprocess.check_output(['nmcli', '-t', '-f', 'NAME,TYPE,DEVICE', 'con', 'show', '--active'], text=True)
        lines = result.strip().split('\n')
        for line in lines:
            parts = line.strip().split(':')
            if len(parts) < 3:
                continue
            name, ctype, device = parts
            if ctype == 'wifi':
                print(f"🚫 Disconnecting Wi-Fi connection: {name}")
                subprocess.check_call(['nmcli', 'con', 'down', name])
                return jsonify({'status': 'ok', 'message': f'Disconnected from {name}'})
        print("⚠️ No connection name found — disconnecting wlan0 directly...")
        subprocess.check_call(['nmcli', 'device', 'disconnect', 'wlan0'])
        return jsonify({'status': 'ok', 'message': 'Disconnected wlan0'})
    except subprocess.CalledProcessError as e:
        print(f"❌ Wi-Fi disconnect error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/launch_keyboard', methods=['POST'])
def launch_keyboard():
    try:
        env = os.environ.copy()
        env['DISPLAY'] = ':0'
        env['XAUTHORITY'] = '/home/pi/.Xauthority'
        subprocess.Popen(['onboard'], env=env)
        return jsonify({"status": "launched"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/hide_keyboard', methods=['POST'])
def hide_keyboard():
    try:
        subprocess.call(['pkill', '-f', 'onboard'])
        return jsonify({"status": "hidden"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/sounds')
def list_sounds():
    sound_dir = os.path.join('static', 'sounds')
    try:
        files = [
            f
            for f in os.listdir(sound_dir)
            if f.lower().endswith(('.mp3', '.wav'))
        ]
        return jsonify({'files': files})
    except Exception as e:
        return jsonify({'files': [], 'error': str(e)}), 500

@app.route('/api/version')
def api_version():
    try:
        with open("version.txt") as f:
            return jsonify({"version": f.read().strip()})
    except:
        return jsonify({"version": "Unknown"})

@app.route("/release-summary")
def release_summary_page():
    return send_from_directory('static', 'release-summary.html')

@app.route("/release-summary-data")
def release_summary_data():
    try:
        tournament = get_current_tournament()
        settings = load_settings()
        demo_mode = settings.get("data_source") == "demo"
        if demo_mode:
            data = load_demo_data(tournament)
            all_events = data.get("events", [])
            eastern = ZoneInfo("America/New_York")
            now = datetime.now(eastern)
            events = []
            for e in all_events:
                try:
                    ts = date_parser.parse(e["timestamp"])
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=eastern)
                    else:
                        ts = ts.astimezone(eastern)
                    event_today = datetime.combine(now.date(), ts.timetz())
                except Exception:
                    continue
                if event_today <= now:
                    events.append(e)
        else:
            events_file = get_cache_path(tournament, "events.json")
            events = safe_json_load(events_file, [])

        summary = defaultdict(lambda: {"blue_marlins": 0, "white_marlins": 0, "sailfish": 0, "total_releases": 0})

        for e in events:
            if e["event"].lower() != "released":
                continue
            try:
                dt = date_parser.parse(e["timestamp"])
                day = dt.strftime("%Y-%m-%d")
            except:
                continue
            details = e.get("details", "").lower()
            if "blue marlin" in details:
                summary[day]["blue_marlins"] += 1
            elif "white marlin" in details:
                summary[day]["white_marlins"] += 1
            elif "sailfish" in details:
                summary[day]["sailfish"] += 1
            summary[day]["total_releases"] += 1

        result = [{"date": k, **v} for k, v in sorted(summary.items(), key=lambda x: x[0], reverse=True)]
        return jsonify({"status": "ok", "demo_mode": demo_mode,
                        "summary": result, "events": events})
    except Exception as e:
        print(f"❌ Error generating release summary: {e}")
        return jsonify({"status": "error", "message": str(e)})

# ------------------------
# Followed boats
# ------------------------
@app.route('/followed-boats', methods=['GET'])
def get_followed_boats_api():
    settings = load_settings()
    return jsonify(settings.get("followed_boats", []))

@app.route('/followed-boats/toggle', methods=['POST'])
def toggle_followed_boat():
    data = request.get_json()
    boat = data.get("boat")
    if not boat:
        return jsonify({"status": "error", "message": "Missing 'boat'"}), 400
    settings = load_settings()
    followed = settings.get("followed_boats", [])
    uid = normalize_boat_name(boat)
    followed_norm = [normalize_boat_name(b) for b in followed]
    if uid in followed_norm:
        followed = [b for b in followed if normalize_boat_name(b) != uid]
        action = "unfollowed"
    else:
        followed.append(boat)
        action = "followed"
    settings["followed_boats"] = followed
    safe_json_dump(SETTINGS_FILE, settings)
    return jsonify({"status": "ok", "action": action, "followed_boats": followed})

# ------------------------
# Startup
# ------------------------
def startup_scrape():
    mode = get_data_source()
    tournament = get_current_tournament()
    cache = load_cache()
    if mode == "live":
        print(f"🔄 Startup: Checking caches for live mode tournament {tournament}")
        participants_file = get_cache_path(tournament, "participants.json")
        events_file = get_cache_path(tournament, "events.json")
        lb_file = get_cache_path(tournament, "leaderboard.json")
        part_key = f"{tournament}_participants"
        event_key = f"events_{tournament}"
        lb_key = f"leaderboard_{tournament}"
        if not os.path.exists(participants_file) or not is_cache_fresh(cache, part_key, 1440):
            run_in_thread(scrape_participants, "participants")
        if not os.path.exists(events_file) or not is_cache_fresh(cache, event_key, 2):
            run_in_thread(lambda: scrape_events(tournament=tournament), "events")
        if not os.path.exists(lb_file) or not is_cache_fresh(cache, lb_key, 2):
            run_in_thread(lambda: scrape_leaderboard(tournament), "leaderboard")
    elif mode == "demo":
        print(f"🔄 Startup: Checking demo cache for {tournament}")
        data = load_demo_data(tournament)
        if not data.get("events"):
            run_in_thread(lambda: build_demo_cache(tournament), "demo_cache")

if __name__ == '__main__':
    print("🚀 Starting (stabilized, faster fetch, fixed leaderboard).")
    Thread(target=background_event_emailer, daemon=True).start()
    startup_scrape()
    app.run(host='0.0.0.0', port=5000, debug=True)
