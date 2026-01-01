open("/tmp/pfsense-autologin.PROOF", "a").write("script started\n")

import requests
import time
import configparser
import logging
import os
import subprocess
import argparse
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "autologin.log")

# --------------------------------------------------
# Logging
# --------------------------------------------------
def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler()
        ]
    )

# --------------------------------------------------
# Config
# --------------------------------------------------
def load_config():
    path = os.path.join(SCRIPT_DIR, "config.ini")
    if not os.path.exists(path):
        raise FileNotFoundError("config.ini not found")

    cfg = configparser.ConfigParser()
    cfg.read(path)
    return cfg

# --------------------------------------------------
# Wi-Fi interface detection (macOS)
# --------------------------------------------------
def wifi_interface():
    try:
        out = subprocess.check_output(
            ["networksetup", "-listallhardwareports"],
            text=True
        )
        lines = out.splitlines()
        for i in range(len(lines)):
            if lines[i].strip() == "Hardware Port: Wi-Fi":
                return lines[i + 1].split(": ")[1]
    except Exception:
        pass
    return None

# --------------------------------------------------
# Current SSID (best effort)
# --------------------------------------------------
def current_ssid():
    iface = wifi_interface()
    if not iface:
        return None
    try:
        out = subprocess.check_output(
            ["networksetup", "-getairportnetwork", iface],
            stderr=subprocess.DEVNULL,
            text=True
        )
        if "Current Wi-Fi Network:" in out:
            return out.split(": ", 1)[1].strip()
    except Exception:
        pass
    return None

# --------------------------------------------------
# Internet connectivity check
# --------------------------------------------------
def internet_up(check_url):
    try:
        r = requests.get(check_url, timeout=5)
        return r.status_code == 204
    except requests.RequestException:
        return False

# --------------------------------------------------
# Portal availability
# --------------------------------------------------
def portal_available(url):
    try:
        r = requests.get(url, timeout=5, verify=False)
        return r.status_code == 200
    except requests.RequestException:
        return False

# --------------------------------------------------
# pfSense captive portal login
# --------------------------------------------------
def login_pfsense(session, login_url, username, password, check_url):
    payload = {
        "auth_user": username,
        "auth_pass": password,
        "redirurl": "https://www.google.com",
        "accept": "Login"
    }

    headers = {
        "User-Agent": "pfSense captive portal autologin",
        "Referer": login_url
    }

    logging.info("Attempting captive portal login")

    try:
        session.post(
            login_url,
            data=payload,
            headers=headers,
            timeout=10,
            verify=False,
            allow_redirects=True
        )
    except requests.RequestException as e:
        logging.error(f"Login POST failed: {e}")
        return False

    time.sleep(2)

    try:
        test = session.get(check_url, timeout=5)
        if test.status_code == 204:
            logging.info("Login successful (internet access confirmed)")
            return True
    except requests.RequestException:
        pass

    logging.error("Login failed (still captive)")
    return False

# --------------------------------------------------
# Main logic
# --------------------------------------------------
def run(once=False):
    logging.info("LaunchAgent triggered, evaluating network state")

    cfg = load_config()

    username = cfg["credentials"]["username"]
    password = cfg["credentials"]["password"]
    portal_url = cfg["settings"]["portal_url"]
    check_url = cfg["settings"]["connectivity_check_url"]
    ssid_required = cfg["settings"].get("ssid", "").strip()

    base_delay = int(cfg["settings"]["retry_delay"])
    max_delay = 300  # 5 minutes

    session = requests.Session()
    retry_delay = base_delay

    logging.info("pfSense captive portal auto-login started")

    while True:
        # ---------- SSID GUARD (corrected) ----------
        if ssid_required:
            ssid = current_ssid()

            # Only block if SSID is known AND wrong
            if ssid is not None and ssid != ssid_required:
                logging.info(
                    f"SSID '{ssid}' does not match '{ssid_required}', waiting"
                )
                if once:
                    return
                time.sleep(10)
                continue

        # ---------- Connectivity ----------
        if internet_up(check_url):
            retry_delay = base_delay
            if once:
                logging.info("Internet already up, exiting (--once)")
                return
            time.sleep(30)
            continue

        logging.warning("Internet DOWN")

        # ---------- Portal + Login ----------
        if portal_available(portal_url):
            if login_pfsense(session, portal_url, username, password, check_url):
                retry_delay = base_delay
                if once:
                    return
                time.sleep(10)
                continue
        else:
            logging.warning("Captive portal not reachable")

        # ---------- Backoff ----------
        logging.warning(f"Retrying in {retry_delay} seconds")
        time.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, max_delay)

# --------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    args = parser.parse_args()

    setup_logging()

    try:
        run(once=args.once)
    except KeyboardInterrupt:
        logging.info("Stopped by user")
    except Exception as e:
        logging.critical(f"Fatal error: {e}", exc_info=True)
