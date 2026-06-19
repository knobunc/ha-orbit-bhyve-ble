#!/usr/bin/env python3
"""Standalone CLI to test Orbit B-Hyve cloud login and device discovery.

Usage:
    python3 test_cloud.py EMAIL PASSWORD
    python3 test_cloud.py                   # prompts for credentials
"""
import asyncio
import base64
import json
import sys
from getpass import getpass

import aiohttp

API_BASE = "https://api.orbitbhyve.com/v1"
APP_ID = "Bhyve-App"
KEY_PATHS = ("/meshes/{mesh_id}", "/network_topologies/{mesh_id}", "/networks/{mesh_id}")
KEY_FIELDS = ("ble_network_key", "network_key")


def _format_mac(raw: str | None) -> str | None:
    if not raw or len(raw) != 12:
        return None
    return ":".join(raw[i:i+2] for i in range(0, 12, 2)).upper()


def _b64_to_hex(b64: str | None) -> str | None:
    if not b64:
        return None
    try:
        return base64.b64decode(b64).hex()
    except Exception:
        return None


async def run(email: str, password: str):
    headers = {"orbit-app-id": APP_ID, "Content-Type": "application/json"}

    async with aiohttp.ClientSession() as session:
        # --- Login ---
        print(f"\n[1] POST {API_BASE}/session")
        print(f"    email: {email}")
        async with session.post(
            f"{API_BASE}/session",
            json={"session": {"email": email, "password": password}},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            print(f"    status: {resp.status}")
            body = await resp.json()
            if resp.status != 200:
                print(f"    response: {json.dumps(body, indent=2)}")
                print("\n    LOGIN FAILED")
                return
            token = body.get("orbit_api_key")
            user_id = body.get("user_id")
            print(f"    user_id: {user_id}")
            print(f"    token: {token[:12]}..." if token else "    token: MISSING")

        if not token:
            print("\n    No orbit_api_key in response. Full response:")
            print(json.dumps(body, indent=2))
            return

        auth_headers = {**headers, "orbit-api-key": token}

        # --- List devices ---
        print(f"\n[2] GET {API_BASE}/devices")
        async with session.get(
            f"{API_BASE}/devices",
            headers=auth_headers,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            print(f"    status: {resp.status}")
            if resp.status != 200:
                print(f"    response: {await resp.text()}")
                return
            devices = await resp.json()

        print(f"    found {len(devices)} device(s)\n")

        for i, d in enumerate(devices):
            dtype = d.get("type", "?")
            name = d.get("name", "?")
            hw = d.get("hardware_version", "?")
            fw = d.get("firmware_version", "?")
            mac = _format_mac(d.get("mac_address"))
            mesh_id = d.get("mesh_id") or d.get("network_topology_id")
            stations = d.get("num_stations", "?")
            connected = d.get("is_connected", "?")
            battery = d.get("battery", {})

            print(f"    Device {i+1}: {name}")
            print(f"      type:      {dtype}")
            print(f"      hardware:  {hw}")
            print(f"      firmware:  {fw}")
            print(f"      mac:       {mac}")
            print(f"      mesh_id:   {mesh_id}")
            print(f"      stations:  {stations}")
            print(f"      connected: {connected}")
            if battery:
                print(f"      battery:   {battery}")

            if dtype == "bridge":
                print(f"      (bridge — skipping key lookup)\n")
                continue

            # --- Fetch mesh / network key ---
            if not mesh_id:
                print(f"      WARNING: no mesh_id — can't fetch key\n")
                continue

            key_hex = None
            for path_tmpl in KEY_PATHS:
                path = path_tmpl.format(mesh_id=mesh_id)
                url = f"{API_BASE}{path}"
                async with session.get(
                    url,
                    headers=auth_headers,
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    if resp.status == 404:
                        print(f"      {path}: 404")
                        continue
                    if resp.status != 200:
                        print(f"      {path}: {resp.status}")
                        continue
                    mesh = await resp.json()
                    print(f"      {path}: 200")
                    for field in KEY_FIELDS:
                        val = mesh.get(field)
                        if val:
                            key_hex = _b64_to_hex(val)
                            print(f"      {field}: {val} -> {key_hex}")
                    break

            if key_hex:
                print(f"      NETWORK KEY: {key_hex}")
            else:
                print(f"      WARNING: no network key found")
            print()

        print("Done.")


def main():
    if len(sys.argv) >= 3:
        email, password = sys.argv[1], sys.argv[2]
    else:
        email = input("Email: ")
        password = getpass("Password: ")

    asyncio.run(run(email, password))


if __name__ == "__main__":
    main()
