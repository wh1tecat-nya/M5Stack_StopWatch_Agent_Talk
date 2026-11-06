import argparse
import getpass
import json
import os
from pathlib import Path
import string
import sys
from urllib.parse import urlsplit


def cpp_string(value):
    return '"' + "".join(f"\\x{byte:02x}" for byte in value.encode("utf-8")) + '"'


def main():
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Write the local StopWatch Wi-Fi configuration")
    parser.add_argument("root_ca", type=Path)
    parser.add_argument("--relay-config", type=Path, default=root / "cloudflare/.env.text-probe.json")
    args = parser.parse_args()
    if not sys.stdin.isatty():
        parser.error("Run this command in a terminal")
    config = root / "SumiAvatar/stopwatch_config.h"
    if config.exists():
        parser.error("stopwatch_config.h already exists")
    relay = json.loads(args.relay_config.read_text())
    origin = relay["RELAY_ORIGIN"]
    endpoint = urlsplit(origin)
    if endpoint.scheme != "https" or not endpoint.hostname or endpoint.username is not None or any(
        (endpoint.path, endpoint.query, endpoint.fragment)
    ):
        parser.error("The relay origin must be an HTTPS origin")
    ca = args.root_ca.read_text()
    if not ca.startswith("-----BEGIN CERTIFICATE-----") or ')PEM"' in ca:
        parser.error("Expected a PEM root certificate")
    ssid = input("2.4 GHz Wi-Fi SSID: ")
    password = getpass.getpass("Wi-Fi password: ")
    valid_psk = len(password) == 64 and all(char in string.hexdigits for char in password)
    if not 1 <= len(ssid.encode()) <= 32 or (len(password.encode()) > 63 and not valid_psk):
        parser.error("Invalid Wi-Fi SSID or password length")
    values = {
        "kWifiSsid": ssid,
        "kWifiPassword": password,
        "kRelayUrl": origin,
        "kRelayToken": relay["DEVICE_TOKEN"],
    }
    with os.fdopen(os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
        output.write("#pragma once\n\n")
        for name, value in values.items():
            output.write(f"static constexpr char {name}[] = {cpp_string(value)};\n")
        output.write(f'static constexpr char kRelayRootCa[] = R"PEM(\n{ca.strip()}\n)PEM";\n')
    print("Created SumiAvatar/stopwatch_config.h")


if __name__ == "__main__":
    main()
