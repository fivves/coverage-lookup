#!/usr/bin/env python3
"""Coverage Lookup — address-level cell coverage from FCC filings.

Type an address or a business name, get the FCC's per-carrier mobile coverage at
that point: technology, environment, predicted minimum signal in dBm, and the
minimum speeds each carrier commits to.

Run it (from the NAS, or over SSH):
    ~/.hermes/hermes-agent/venv/bin/python server.py
    ~/.hermes/hermes-agent/venv/bin/python server.py --host 0.0.0.0 --port 8790

It needs the Hermes venv python only for `websockets` (used to talk to the
headless Chrome that fronts the FCC's CDN-protected API).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fcc_client import FccClient, FccError, strength_label, summarize

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
GEO_UA = "coverage-lookup/1.0"

CACHE_TTL = 3600
_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()

# Simple per-IP guard: the app will be public via the tunnel, and every lookup costs a
# browser round-trip. 60 lookups an hour is far more than a rep on a call needs.
RATE_LIMIT = 60
RATE_WINDOW = 3600
_hits: dict[str, list[float]] = {}
_hits_lock = threading.Lock()


def rate_ok(ip: str) -> bool:
    now = time.time()
    with _hits_lock:
        stamps = [t for t in _hits.get(ip, []) if now - t < RATE_WINDOW]
        if len(stamps) >= RATE_LIMIT:
            _hits[ip] = stamps
            return False
        stamps.append(now)
        _hits[ip] = stamps
    return True

SUGGEST_TTL = 900
_suggest_cache: dict[str, tuple[float, list[dict]]] = {}
_SUGGEST_LIMIT = 1200  # per IP per hour; a typeahead fires more often than a lookup
_suggest_hits: dict[str, list[float]] = {}

client: FccClient | None = None
client_lock = threading.Lock()


# ------------------------------------------------------------------ geocoding


def _get_json(url: str, timeout: int = 25) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": GEO_UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def geocode_census(query: str) -> dict | None:
    q = urllib.parse.urlencode(
        {"address": query, "benchmark": "Public_AR_Current", "format": "json"}
    )
    try:
        data = _get_json(
            f"https://geocoding.geo.census.gov/geocoder/locations/onelineaddress?{q}"
        )
    except Exception:
        return None
    matches = data.get("result", {}).get("addressMatches") or []
    if not matches:
        return None
    m = matches[0]
    return {
        "matched": m.get("matchedAddress", ""),
        "lat": m["coordinates"]["y"],
        "lon": m["coordinates"]["x"],
        "source": "US Census",
    }


def geocode_nominatim(query: str, limit: int = 5) -> list[dict]:
    q = urllib.parse.urlencode(
        {"q": query, "format": "jsonv2", "limit": limit, "addressdetails": 1}
    )
    try:
        data = _get_json(f"https://nominatim.openstreetmap.org/search?{q}")
    except Exception:
        return []
    return [
        {
            "matched": r.get("display_name", ""),
            "lat": float(r["lat"]),
            "lon": float(r["lon"]),
            "source": "OpenStreetMap",
            "type": r.get("type", ""),
        }
        for r in data
    ]


def looks_like_address(q: str) -> bool:
    first = q.strip().split(" ")[0]
    return first[:1].isdigit()


def resolve(query: str) -> tuple[dict | None, list[dict]]:
    """Return (best hit, alternatives). Addresses go to Census first."""
    candidates: list[dict] = []
    if looks_like_address(query):
        hit = geocode_census(query)
        if hit:
            candidates.append(hit)
    if not candidates:
        candidates = geocode_nominatim(query)
        if not candidates:
            hit = geocode_census(query)
            if hit:
                candidates.append(hit)
    if not candidates:
        return None, []
    return candidates[0], candidates[1:]


# ------------------------------------------------------------------ FCC layer


def suggest_ok(ip: str) -> bool:
    now = time.time()
    with _hits_lock:
        stamps = [t for t in _suggest_hits.get(ip, []) if now - t < RATE_WINDOW]
        if len(stamps) >= _SUGGEST_LIMIT:
            _suggest_hits[ip] = stamps
            return False
        stamps.append(now)
        _suggest_hits[ip] = stamps
    return True


def photon_suggest(query: str, limit: int = 8) -> list[dict]:
    """OpenStreetMap-backed typeahead (Photon): businesses, landmarks, cities, ZIPs.

    The FCC fabric only holds street addresses, so a caller who types a company name gets
    nothing from it. Photon fills that gap. Results are filtered to the US and labelled the
    way the lookup expects to receive them.
    """
    q = urllib.parse.urlencode({"q": query, "limit": limit})
    try:
        data = _get_json(f"https://photon.komoot.io/api/?{q}")
    except Exception:
        return []
    out = []
    for feat in data.get("features") or []:
        props = feat.get("properties") or {}
        if props.get("countrycode") and props["countrycode"] != "US":
            continue
        parts = []
        name = props.get("name")
        street = " ".join(str(x) for x in [props.get("housenumber"), props.get("street")] if x)
        if name and name not in (street or ""):
            parts.append(name)
        if street:
            parts.append(street)
        for key in ("city", "state", "postcode"):
            if props.get(key):
                parts.append(props[key])
        label = ", ".join(parts)
        if label:
            out.append({"label": label, "source": "osm"})
    return out[:limit]


_nominatim_last = 0.0


def nominatim_suggest(query: str, limit: int = 6) -> list[dict]:
    """Slower OSM geocoder, used only when Photon comes back thin.

    Nominatim's usage policy forbids aggressive autocomplete, so this is a fallback behind
    a 1 req/sec throttle and the same 15 minute cache as everything else.
    """
    global _nominatim_last
    with _hits_lock:
        wait = 1.1 - (time.time() - _nominatim_last)
        if wait > 0:
            time.sleep(wait)
        _nominatim_last = time.time()
    q = urllib.parse.urlencode(
        {"q": query, "format": "jsonv2", "limit": limit, "countrycodes": "us", "addressdetails": 1}
    )
    try:
        data = _get_json(f"https://nominatim.openstreetmap.org/search?{q}")
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for row in data:
        label = row.get("display_name") or ""
        if not label:
            continue
        parts = [p.strip() for p in label.split(",")]
        # drop the trailing ", United States"
        if parts and parts[-1].lower() in ("united states", "usa", "us"):
            parts = parts[:-1]
        out.append({"label": ", ".join(parts[:5]), "source": "osm"})
    return out


def suggest(query: str) -> list[dict]:
    key = query.strip().lower()
    with _cache_lock:
        hit = _suggest_cache.get(key)
        if hit and time.time() - hit[0] < SUGGEST_TTL:
            return hit[1]

    results: list[dict] = []
    address_like = key[:1].isdigit()
    house_number = key.split()[0] if address_like else ""
    # A leading digit means a street address, where the FCC's own fabric is authoritative.
    if address_like and len(key.split()) > 1:
        try:
            results = get_client().address_suggest(query)
        except FccError:
            results = []
    wants_fcc = address_like and len(key.split()) > 1  # a bare ZIP is not a street address
    if wants_fcc and not results:
        results = []
    elif not wants_fcc:
        results = []

    if len(results) < 4:
        osm = photon_suggest(query)
        if len(osm) < 2:
            osm += nominatim_suggest(query)
        for cand in osm:
            # For an address query keep only candidates that contain every token typed, so
            # "240 frenchys cove" cannot return coves in four other states. Showing nothing
            # is more honest than showing the wrong place.
            if address_like and not all(tok in cand["label"].lower() for tok in key.split()):
                continue
            results.append(cand)

    seen, merged = set(), []
    for item in results:
        label = item["label"].strip()
        norm = re.sub(r"[^a-z0-9]+", "", label.lower())  # collapses punctuation/comma variants
        if not label or norm in seen:
            continue
        seen.add(norm)
        merged.append({"label": label, "source": item["source"]})
        if len(merged) >= 7:
            break

    with _cache_lock:
        _suggest_cache[key] = (time.time(), merged)
    return merged


def get_client() -> FccClient:
    global client
    with client_lock:
        if client is None:
            client = FccClient()
            client.start()
        return client


def warm() -> None:
    try:
        get_client()
    except Exception as e:  # noqa: BLE001
        print(f"[warm] browser not ready yet: {e}", flush=True)


def lookup(query: str) -> dict:
    key = query.strip().lower()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            cached = dict(hit[1])
            cached["cached"] = True
            return cached

    best, alternatives = resolve(query)
    if not best:
        return {
            "ok": False,
            "error": "No match for that address or place name.",
            "hint": "Try adding the city and ZIP, or drop the unit number.",
        }

    detail = get_client().mobile_detail(best["lat"], best["lon"])
    carriers = summarize(detail)
    for c in carriers:
        c["label"] = strength_label(c.get("best_outdoor_dbm") or c.get("best_dbm"))
        for r in c["rows"]:
            r["label"] = strength_label(r.get("dbm"))

    result = {
        "ok": True,
        "query": query,
        "location": best,
        "alternatives": alternatives[:4],
        "vintage": detail["vintage"],
        "carriers": carriers,
        "fcc_url": (
            "https://broadbandmap.fcc.gov/location-summary/mobile"
            f"?lon={best['lon']:.6f}&lat={best['lat']:.6f}"
        ),
        "fetched_at": time.strftime("%Y-%m-%d %H:%M"),
    }
    with _cache_lock:
        _cache[key] = (time.time(), result)
    return result


# ------------------------------------------------------------------ HTTP


class Handler(BaseHTTPRequestHandler):
    server_version = "CoverageLookup/1.0"

    def log_message(self, fmt, *args):  # keep the console readable
        print(f"{self.address_string()} {fmt % args}", flush=True)

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: str) -> None:
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404)
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".ico": "image/x-icon",
        }.get(os.path.splitext(path)[1], "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._file(os.path.join(STATIC, "index.html"))
            return
        # static assets (config.js and friends), filename-checked, no path traversal
        name = os.path.basename(parsed.path.lstrip("/"))
        if name and re.fullmatch(r"[A-Za-z0-9._-]+", name):
            candidate = os.path.join(STATIC, name)
            if os.path.isfile(candidate):
                self._file(candidate)
                return
        if parsed.path == "/healthz":
            self._json({"ok": True, "time": time.strftime("%Y-%m-%dT%H:%M:%S")})
            return
        if parsed.path == "/api/suggest":
            qs = urllib.parse.parse_qs(parsed.query)
            q = (qs.get("q") or [""])[0].strip()
            ip = self.headers.get("CF-Connecting-IP") or self.client_address[0]
            if not suggest_ok(ip):
                self._json({"ok": True, "suggestions": []})  # never break typing over a limit
                return
            if len(q) < 3:
                self._json({"ok": True, "suggestions": []})
                return
            try:
                self._json({"ok": True, "suggestions": suggest(q)})
            except Exception:
                self._json({"ok": True, "suggestions": []})
            return
        if parsed.path == "/api/lookup":
            qs = urllib.parse.parse_qs(parsed.query)
            q = (qs.get("q") or [""])[0].strip()
            # Behind the tunnel the socket peer is cloudflared, so prefer Cloudflare's client IP.
            ip = self.headers.get("CF-Connecting-IP") or self.client_address[0]
            if not rate_ok(ip):
                self._json(
                    {"ok": False, "error": f"Too many lookups from this address (limit {RATE_LIMIT}/hour)."},
                    429,
                )
                return
            if not q:
                self._json({"ok": False, "error": "Give an address or a place name."}, 400)
                return
            try:
                self._json(lookup(q))
            except FccError as e:
                self._json(
                    {"ok": False, "error": f"FCC lookup failed: {e}", "hint": "Try again in a few seconds."},
                    502,
                )
            except Exception as e:  # noqa: BLE001
                self._json({"ok": False, "error": f"Unexpected error: {e}"}, 500)
            return
        self.send_error(404)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces, needed for the Cloudflare tunnel)")
    ap.add_argument("--port", type=int, default=8790)
    args = ap.parse_args()

    threading.Thread(target=warm, daemon=True).start()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Coverage Lookup on http://{args.host}:{args.port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if client:
            client.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
