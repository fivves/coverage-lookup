# Coverage Lookup

Type an address or a business name, get the FCC's per-carrier mobile coverage at that point:
technology (4G LTE / 5G-NR), environment (outdoor stationary / in-vehicle), the **predicted
minimum signal in dBm**, and the minimum down/up speeds each carrier commits to — for AT&T,
T-Mobile and Verizon, side by side.

No API keys, no accounts, no cost. One page, one input, one result.

## Why this exists

The carriers' own coverage maps show a single optimistic heat map and never give you a number.
The FCC's National Broadband Map has the real per-carrier data but buries it behind a JS map
that will not accept a pasted address cleanly. This wraps that data in the one thing a support
rep actually needs: type an address, get a ranked answer in under a second.

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install websockets
.venv/bin/python server.py                 # http://0.0.0.0:8790
```

`websockets` is the only dependency; everything else is the standard library. It is used to
drive the headless Chrome that fronts the FCC's CDN-protected API (see below).

Options: `--host` (default `0.0.0.0`), `--port` (default `8790`).

There is a systemd user unit in `coverage-lookup.service` if you want it persistent:

```bash
cp coverage-lookup.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now coverage-lookup.service
```

## Branding without editing tracked files

`static/config.example.js` is the template. Copy it to `static/config.js` (gitignored) to set
your own app name, prompt and example addresses:

```js
window.COVERAGE_UI = {
  name: "Coverage Lookup",
  prompt: "user@host",
  example: "350 5th Ave, New York, NY 10118",
  example2: "Costco Camarillo",
};
```

Without that file the page falls back to neutral defaults.

## How it works (and why it looks like this)

The FCC's public map has no documented API, and its data endpoints are behind Akamai, which
returns 403 to `curl` and to a stock headless Chrome. So `fcc_client.py` keeps **one headless
Chrome alive**, loads `broadbandmap.fcc.gov`, and does the JSON fetches from inside the page,
where the request carries the cookies and TLS fingerprint the CDN expects. Presenting as
ordinary desktop Chrome plus `--disable-blink-features=AutomationControlled` is what makes it
work; the stock headless UA is refused.

Endpoints (discovered from the site's own XHR traffic):

- `GET /nbm/map/api/published/filing` → filings, each with a `process_uuid`
- `GET /nbm/map/api/mobile/detail/<process_uuid>/<lat>/<lon>` → rows of provider + technology

Each row carries `minsignal` (dBm), `mindown`/`minup` (Mbps) and `environmnt`
(**0 = Outdoor Stationary, 1 = In-Vehicle Mobile**, taken from the site's own `envNames` map).

Geocoding: US Census first (best for US street addresses, free, no key), OpenStreetMap
Nominatim as the fallback for business names. Business names are a geocoding problem, not a
coverage problem — small businesses that are not in OSM need a real street address.

## What it does NOT do

- It is not a measurement. It is the carrier's filed prediction: outdoor, ground level, with a
  six-month lag (the filing published at time of writing is December 31, 2025).
- It says nothing about indoors, terrain, congestion at 8pm, or which bands a given modem
  supports. For tower geometry and bands, use CellMapper.
- "Predicted minimum" is deliberately conservative: it is the floor the carrier commits to at
  that point, so real hardware on site may do better.
- Single-address lookups only. No bulk import, no accounts, no history.

## Files

- `server.py` — HTTP server, geocoding, result shaping (stdlib + `websockets`)
- `fcc_client.py` — headless Chrome + CDP client for the FCC endpoints, plus ranking helpers
- `static/index.html` — the whole front end, no build step, no dependencies
- `static/config.example.js` — copy to `config.js` to override branding
- `add-tunnel-host.py` — publish a port through an existing Cloudflare tunnel (DNS + ingress)
- `coverage-lookup.service` — systemd user unit

## Gotchas learned the hard way

- **An orphaned headless Chrome locks the profile** and the next launch dies with "headless
  Chrome did not come up". `start()` clears stale `Singleton*` files and kills any browser
  still holding the profile; `stop()` kills the whole process group. Manual fix:
  `pkill -f chrome-profile`.
- **The FCC CDN (Akamai) 403s the stock headless UA.** The desktop UA plus
  `--disable-blink-features=AutomationControlled` is load-bearing; do not "clean it up".
- **A Cloudflare tunnel ingress `PUT` replaces the whole config.** `add-tunnel-host.py`
  snapshots it to `~/.hermes/state/` first; rollback is a `PUT` of that file. Rules carrying
  `originRequest` must keep it, and the catch-all rule must stay last.
- **Do not pass a unit number to the FCC site** ("Apt 46" fails its location-fabric lookup).
  Geocode first and query by coordinates — which is exactly what this app does.
