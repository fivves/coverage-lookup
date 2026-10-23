#!/usr/bin/env python3
"""Publish a local port through an existing Cloudflare tunnel (DNS CNAME + ingress rule).

Usage:
    ./add-tunnel-host.py map.example.com --port 8790

Reads CLOUDFLARE_API_TOKEN, CLOUDFLARE_TUNNEL_ID and a zone id from the environment
(or from ~/.hermes/.env if they are not exported). Snapshots the current ingress config
before writing. Never prints credential values.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

ENV_PATH = os.path.expanduser("~/.hermes/.env")
API = "https://api.cloudflare.com/client/v4"


def env_value(key: str, required: bool = True) -> str:
    with open(ENV_PATH, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    if required:
        raise SystemExit(f"{key} not found in the environment or .env")
    return ""


def call(method: str, path: str, token: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        API + path,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        payload = e.read().decode()
        raise SystemExit(f"{method} {path} -> HTTP {e.code}: {payload[:400]}")


def main() -> int:
    global HOSTNAME, LOCAL_PORT
    ap = argparse.ArgumentParser(description="Point a tunnel hostname at a local port")
    ap.add_argument("hostname", help="e.g. app.example.com")
    ap.add_argument("--port", type=int, required=True, help="local port the service listens on")
    ap.add_argument("--zone-id", default=os.environ.get("CLOUDFLARE_ZONE_ID") or env_value("CLOUDFLARE_ZONE_ID", required=False))
    ap.add_argument("--account-id", default=os.environ.get("CLOUDFLARE_ACCOUNT_ID") or env_value("CLOUDFLARE_ACCOUNT_ID", required=False))
    args = ap.parse_args()
    HOSTNAME, LOCAL_PORT = args.hostname, args.port

    token = os.environ.get("CLOUDFLARE_API_TOKEN") or env_value("CLOUDFLARE_API_TOKEN")
    tunnel_id = os.environ.get("CLOUDFLARE_TUNNEL_ID") or env_value("CLOUDFLARE_TUNNEL_ID")
    zone_id = args.zone_id
    account_id = args.account_id
    if not zone_id or not account_id:
        raise SystemExit("need --zone-id and --account-id (or CLOUDFLARE_ZONE_ID / CLOUDFLARE_ACCOUNT_ID)")

    # ---- 1. snapshot existing ingress
    cfg = call("GET", f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations", token)
    config = cfg["result"]["config"]
    rules = config.get("ingress", [])
    stamp = time.strftime("%Y%m%d-%H%M%S")
    snap = os.path.expanduser(f"~/.hermes/state/tunnel-ingress-backup-{stamp}.json")
    os.makedirs(os.path.dirname(snap), exist_ok=True)
    with open(snap, "w", encoding="utf-8") as f:
        json.dump(cfg["result"], f, indent=2)
    print(f"ingress snapshot: {snap}  ({len(rules)} rules)")

    existing = [r for r in rules if r.get("hostname") == HOSTNAME]
    print(f"existing rules for {HOSTNAME}: {len(existing)}")

    # ---- 2. DNS record
    dns = call("GET", f"/zones/{zone_id}/dns_records?name={HOSTNAME}", token)
    target = f"{tunnel_id}.cfargotunnel.com"
    if dns.get("result"):
        rec = dns["result"][0]
        print(f"DNS already exists: {rec['type']} -> {rec['content']} (proxied={rec['proxied']})")
        if rec["content"] != target or not rec["proxied"]:
            print("  updating to point at the tunnel, proxied")
            call(
                "PUT",
                f"/zones/{zone_id}/dns_records/{rec['id']}",
                token,
                {"type": "CNAME", "name": HOSTNAME, "content": target, "proxied": True, "ttl": 1},
            )
            print("  DNS updated")
    else:
        call(
            "POST",
            f"/zones/{zone_id}/dns_records",
            token,
            {"type": "CNAME", "name": HOSTNAME, "content": target, "proxied": True, "ttl": 1},
        )
        print(f"DNS created: CNAME {HOSTNAME} -> {target} (proxied)")

    # ---- 3. ingress rule, inserted before the catch-all
    new_rule = {"hostname": HOSTNAME, "service": f"http://localhost:{LOCAL_PORT}"}
    if existing and existing[0].get("service") == new_rule["service"]:
        print("ingress rule already correct, no write needed")
    else:
        rules = [r for r in rules if r.get("hostname") != HOSTNAME]  # drop dead duplicates
        is_catchall = bool(rules) and "hostname" not in rules[-1]
        if is_catchall:
            out = rules[:-1] + [new_rule, rules[-1]]
        else:
            out = rules + [new_rule, {"service": "http_status:404"}]
        config["ingress"] = out
        call("PUT", f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations", token, {"config": config})
        print(f"ingress written ({len(out)} rules, {HOSTNAME} -> {new_rule['service']})")

    # ---- 4. read back
    check = call("GET", f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations", token)
    back = check["result"]["config"]["ingress"]
    hit = [r for r in back if r.get("hostname") == HOSTNAME]
    last = back[-1]
    print(f"read-back: {len(back)} rules, {HOSTNAME} -> {hit[0]['service'] if hit else 'MISSING'}, "
          f"catch-all last = {last.get('service')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
