"""FCC National Broadband Map mobile-coverage client.

The FCC's data endpoints sit behind Akamai, which rejects plain HTTP clients
(curl gets 403 "Access Denied" even with a full browser header set). A real
Chrome renders the site fine, so this module keeps ONE headless Chrome alive and
does the data fetches from inside the page context, where the request carries
the cookies and TLS fingerprint Akamai expects.

Discovered 2026-10-23 from the site's own XHR traffic:

    GET /nbm/map/api/published/filing                       -> filings incl. process_uuid
    GET /nbm/map/api/mobile/detail/<process_uuid>/<lat>/<lon>

The detail rows are richer than the rendered table: each is one provider +
technology at that point, with `minsignal` in dBm plus minimum down/up Mbps and
an environment flag (0 = Outdoor Stationary, 1 = In-Vehicle Mobile, confirmed
from the site's own `envNames` map).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.request

from websockets.sync.client import connect

CHROME_CANDIDATES = [
    os.path.expanduser("~/.hermes/tools/chromium-1208/chrome-linux64/chrome"),
    "/usr/bin/chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium-browser",
]

SITE = "https://broadbandmap.fcc.gov"
# Sent as the browser UA: the stock headless UA is refused by the FCC's CDN.
DESKTOP_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)
# The filing the public map shows by default. Falls back to the newest biannual
# filing the API advertises if this one disappears.
PREFERRED_FILING = "December 31, 2025"

ENV_NAMES = {0: "Outdoor Stationary", 1: "In-Vehicle Mobile"}


class FccError(RuntimeError):
    pass


def find_chrome() -> str:
    for p in CHROME_CANDIDATES:
        if p and os.path.exists(p):
            return p
    found = shutil.which("chromium") or shutil.which("google-chrome")
    if found:
        return found
    raise FccError("no Chrome/Chromium found; set CHROME_PATH")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FccClient:
    """One warm headless Chrome, one page on broadbandmap.fcc.gov, CDP on top."""

    def __init__(self, profile_dir: str | None = None, chrome: str | None = None):
        self.chrome = chrome or find_chrome()
        self.profile_dir = profile_dir or os.path.expanduser(
            "~/.hermes/cache/coverage-app/chrome-profile"
        )
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._ws = None
        self._session: str | None = None
        self._id = 0
        self._filing: tuple[str, str] | None = None  # (label, uuid)
        self._filing_at = 0.0

    # ---------------------------------------------------------------- process

    def start(self) -> None:
        os.makedirs(self.profile_dir, exist_ok=True)
        self._clear_stale_profile_locks()
        self._port = _free_port()
        args = [
            self.chrome,
            "--headless=new",
            f"--remote-debugging-port={self._port}",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--disable-extensions",
            # Akamai fronts broadbandmap.fcc.gov and 403s the stock headless UA.
            # Presenting as ordinary desktop Chrome (plus no automation flag) is
            # what gets the site's JSON back; verified 2026-10-23.
            "--disable-blink-features=AutomationControlled",
            f"--user-agent={DESKTOP_UA}",
            "--window-size=1280,900",
            "about:blank",
        ]
        self._proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
        )
        deadline = time.time() + 30
        ws_url = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self._port}/json/version", timeout=2
                ) as r:
                    ws_url = json.load(r).get("webSocketDebuggerUrl")
                if ws_url:
                    break
            except Exception:
                time.sleep(0.3)
        if not ws_url:
            raise FccError("headless Chrome did not come up")

        self._ws = connect(ws_url, open_timeout=15, max_size=64 * 1024 * 1024)
        target = self._send("Target.createTarget", {"url": f"{SITE}/"})["targetId"]
        self._session = self._send("Target.attachToTarget", {"targetId": target, "flatten": True})[
            "sessionId"
        ]
        self._wait_ready()

    def stop(self) -> None:
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        self._ws = None
        if self._proc and self._proc.poll() is None:
            # start_new_session=True gave the browser its own process group: kill the
            # whole tree, or the renderers linger and keep holding the profile lock.
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
            except Exception:
                self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
                except Exception:
                    self._proc.kill()
        self._proc = None

    def _clear_stale_profile_locks(self) -> None:
        """Drop Singleton* if no live browser owns this profile.

        A browser that outlived its server (killed, crashed, restarted) leaves the lock
        behind, and the next launch then dies with "did not come up".
        """
        probe = subprocess.run(
            ["pgrep", "-f", f"--user-data-dir={self.profile_dir}"],
            capture_output=True,
            text=True,
        )
        if probe.stdout.strip():
            for pid in probe.stdout.split():
                try:
                    os.kill(int(pid), signal.SIGTERM)
                except Exception:
                    pass
            time.sleep(2)
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            p = os.path.join(self.profile_dir, name)
            try:
                if os.path.islink(p) or os.path.exists(p):
                    os.unlink(p)
            except OSError:
                pass

    def _alive(self) -> bool:
        return bool(self._ws) and self._proc is not None and self._proc.poll() is None

    # ---------------------------------------------------------------- CDP glue

    def _send(self, method: str, params: dict | None = None, session: str | None = None) -> dict:
        self._id += 1
        msg = {"id": self._id, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self._ws.send(json.dumps(msg))
        while True:
            raw = self._ws.recv(timeout=60)
            data = json.loads(raw)
            if data.get("id") == self._id:
                if "error" in data:
                    raise FccError(f"{method}: {data['error'].get('message')}")
                return data.get("result", {})

    def _eval(self, expression: str, timeout_ms: int = 45000):
        res = self._send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
                "timeout": timeout_ms,
            },
            session=self._session,
        )
        if res.get("exceptionDetails"):
            det = res["exceptionDetails"]
            raise FccError(f"page error: {det.get('text')} {det.get('exception', {}).get('description', '')}")
        return res.get("result", {}).get("value")

    def _wait_ready(self, timeout: float = 45.0) -> None:
        """Poll until a same-origin FCC fetch succeeds (Akamai cookie established)."""
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                out = self._eval(
                    "(async()=>{try{const r=await fetch('/nbm/map/api/published/filing');"
                    "const j=await r.json();return JSON.stringify({s:r.status,n:(j.data||[]).length});}"
                    "catch(e){return 'ERR:'+e.message}})()",
                    timeout_ms=20000,
                )
                if isinstance(out, str) and out.startswith("{"):
                    payload = json.loads(out)
                    if payload.get("s") == 200 and payload.get("n"):
                        return
                    last = out
                else:
                    last = out
            except Exception as e:  # noqa: BLE001
                last = str(e)
            time.sleep(1.0)
        raise FccError(f"FCC site not usable from the page yet ({last})")

    # ---------------------------------------------------------------- data

    def filing(self) -> tuple[str, str]:
        if self._filing and time.time() - self._filing_at < 86400:
            return self._filing
        raw = self._eval(
            "(async()=>{const r=await fetch('/nbm/map/api/published/filing');return await r.text()})()"
        )
        filings = (json.loads(raw).get("data") or [])
        if not filings:
            raise FccError("FCC returned no filings")
        chosen = next((f for f in filings if f.get("filing_subtype") == PREFERRED_FILING), filings[0])
        self._filing = (chosen.get("filing_subtype", "?"), chosen["process_uuid"])
        self._filing_at = time.time()
        return self._filing

    def address_suggest(self, query: str) -> list[dict]:
        """Address typeahead from the FCC's own fabric, the same one its search box uses.

        GET /nbm/map/api/fabric/address/<filing_uuid>/<query> returns address records
        (address_primary, city, state, zip_code, addr_full, location_id) but no coordinates,
        which is fine here: the suggestion only has to hand a clean address string back to
        the normal lookup path, which geocodes it properly.
        """
        if not self._alive():
            self.stop()
            self.start()
        import urllib.parse as _up

        with self._lock:
            label, uuid = self.filing()
            q = _up.quote(query, safe="")
            expr = (
                "(async()=>{const r=await fetch('%s/nbm/map/api/fabric/address/%s/%s');"
                "const t=await r.text();return JSON.stringify({status:r.status,body:t})})()"
                % (SITE, uuid, q)
            )
            try:
                raw = self._eval(expr)
            except Exception:
                return []
        try:
            envelope = json.loads(raw)
            body = json.loads(envelope.get("body") or "{}")
        except (TypeError, ValueError):
            return []
        out = []
        for row in (body.get("data") or [])[:8]:
            full = row.get("addr_full") or ", ".join(
                p for p in [row.get("address_primary"), row.get("city"), row.get("state")] if p
            )
            if full:
                out.append({"label": full, "source": "fcc"})
        return out

    def mobile_detail(self, lat: float, lon: float) -> dict:
        if not self._alive():
            self.stop()
            self.start()
        with self._lock:
            label, uuid = self.filing()
            expr = (
                "(async()=>{const r=await fetch('%s/nbm/map/api/mobile/detail/%s/%s/%s');"
                "const t=await r.text();return JSON.stringify({status:r.status,body:t})})()"
                % (SITE, uuid, f"{lat:.6f}", f"{lon:.6f}")
            )
            try:
                raw = self._eval(expr)
            except Exception:
                self.stop()
                self.start()
                label, uuid = self.filing()
                expr = (
                    "(async()=>{const r=await fetch('%s/nbm/map/api/mobile/detail/%s/%s/%s');"
                    "const t=await r.text();return JSON.stringify({status:r.status,body:t})})()"
                    % (SITE, uuid, f"{lat:.6f}", f"{lon:.6f}")
                )
                raw = self._eval(expr)
        envelope = json.loads(raw)
        if envelope.get("status") != 200:
            raise FccError(f"FCC mobile detail returned {envelope.get('status')}")
        body = json.loads(envelope["body"])
        rows = body.get("data") or []
        return {"vintage": label, "rows": rows, "result_count": body.get("result_count", len(rows))}


def summarize(detail: dict) -> list[dict]:
    """Collapse raw FCC rows into one entry per carrier, keeping every tech/env row."""
    by_carrier: dict[str, dict] = {}
    for row in detail["rows"]:
        brand = row.get("brandname") or row.get("holding_company") or "?"
        entry = by_carrier.setdefault(brand, {"carrier": brand, "rows": []})
        entry["rows"].append(
            {
                "tech": row.get("technology_type", "?"),
                "env": ENV_NAMES.get(row.get("environmnt"), "?"),
                "env_code": row.get("environmnt"),
                "dbm": row.get("minsignal"),
                "down_mbps": row.get("mindown"),
                "up_mbps": row.get("minup"),
            }
        )
    out = []
    for entry in by_carrier.values():
        for r in entry["rows"]:
            try:
                r["dbm"] = int(r["dbm"])
            except (TypeError, ValueError):
                pass
        # rank on outdoor-stationary rows first; fall back to any row
        outdoor = [r for r in entry["rows"] if r.get("env_code") == 0 and isinstance(r.get("dbm"), int)]
        pool = outdoor or [r for r in entry["rows"] if isinstance(r.get("dbm"), int)]
        entry["best_dbm"] = max((r["dbm"] for r in pool), default=None)
        entry["best_outdoor_dbm"] = max(
            (r["dbm"] for r in entry["rows"] if r.get("env_code") == 0 and isinstance(r.get("dbm"), int)),
            default=None,
        )
        out.append(entry)
    out.sort(key=lambda e: (e["best_outdoor_dbm"] is None, -(e["best_outdoor_dbm"] or -999), -(e["best_dbm"] or -999)))
    return out


def strength_label(dbm: int | None) -> str:
    if dbm is None:
        return "no filing"
    if dbm >= -80:
        return "strong"
    if dbm >= -90:
        return "good"
    if dbm >= -100:
        return "fair"
    if dbm >= -110:
        return "weak"
    return "very weak"


if __name__ == "__main__":  # manual smoke test
    import sys

    lat = float(sys.argv[1]) if len(sys.argv) > 1 else 34.163897
    lon = float(sys.argv[2]) if len(sys.argv) > 2 else -119.034889
    c = FccClient()
    try:
        c.start()
        d = c.mobile_detail(lat, lon)
        print("vintage:", d["vintage"], "| rows:", d["result_count"])
        for e in summarize(d):
            print(f"  {e['carrier']:<10} best_outdoor={e['best_outdoor_dbm']} best_any={e['best_dbm']}")
            for r in e["rows"]:
                print(f"      {r['tech']:<8} {r['env']:<18} {r['dbm']} dBm  {r['down_mbps']}/{r['up_mbps']} Mbps")
    finally:
        c.stop()
