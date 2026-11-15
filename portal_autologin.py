#!/usr/bin/env python3
"""
pfSense Captive Portal Auto-Login with Session Arbitration and macOS Menu Bar Toggle
Supports:
- Mutual exclusion (flock) to prevent overlapping runs
- Dynamic HTML form and CSRF token extraction
- Anti-flapping session arbitration (standby on concurrent session limit / phone conflict)
- Session release (logout) to free slots for other devices
- Atomic state file for macOS Menu Bar (top bar) integration
"""

import os
import sys
import time
import fcntl
import json
import logging
import argparse
import configparser
import subprocess
import warnings
from enum import Enum
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

# Suppress LibreSSL warnings on macOS
warnings.filterwarnings("ignore", message=".*OpenSSL.*")

import urllib3
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "autologin.log")
DEFAULT_CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.ini")
DEFAULT_LOCK_FILE = "/tmp/pfsense-autologin.lock"

# Config directory for persistent state
STATE_DIR = os.path.expanduser("~/.config/pfsense-autologin")
DEFAULT_STATE_FILE = os.path.join(STATE_DIR, "state.json")


# --------------------------------------------------
# Enums and Constants
# --------------------------------------------------
class Status:
    INITIALIZING = "INITIALIZING"
    CONNECTED = "CONNECTED"
    CAPTIVE = "CAPTIVE"
    AUTHENTICATING = "AUTHENTICATING"
    CONFLICT_STANDBY = "CONFLICT_STANDBY"
    AUTH_FAILED = "AUTH_FAILED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    DISCONNECTED = "DISCONNECTED"
    DISABLED = "DISABLED"
    UNKNOWN_NETWORK = "UNKNOWN_NETWORK"


class LoginOutcome(Enum):
    SUCCESS_OR_REDIRECT = "SUCCESS_OR_REDIRECT"
    SESSION_CONFLICT = "SESSION_CONFLICT"
    AUTH_FAILED = "AUTH_FAILED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    CAPTIVE_UNRESOLVED = "CAPTIVE_UNRESOLVED"
    ERROR = "ERROR"


# --------------------------------------------------
# Logging Setup
# --------------------------------------------------
def setup_logging(verbose=False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler(sys.stdout)
        ]
    )


# --------------------------------------------------
# HTML Parser for Captive Portal Forms & CSRF Tokens
# --------------------------------------------------
class PortalFormParser(HTMLParser):
    """
    Parses captive portal HTML to discover the actual form action URL
    and all hidden input fields (such as __csrf_magic, zone, tokens).
    """
    def __init__(self, base_url):
        super().__init__()
        self.base_url = str(base_url or "")
        self.action_url = self.base_url
        self.hidden_fields = {}
        self.in_form = False

    def handle_starttag(self, tag, attrs):
        attr_dict = {str(k).lower(): str(v or "") for k, v in attrs}
        if tag.lower() == "form":
            self.in_form = True
            action = attr_dict.get("action", "")
            if action:
                self.action_url = urljoin(self.base_url, str(action))

        elif tag.lower() == "input":
            input_type = attr_dict.get("type", "").lower()
            name = attr_dict.get("name", "")
            value = attr_dict.get("value", "")
            if name and (input_type == "hidden" or self.in_form):
                if input_type == "hidden":
                    self.hidden_fields[name] = value

    def handle_endtag(self, tag):
        if tag.lower() == "form":
            self.in_form = False


# --------------------------------------------------
# Response Classification (Anti-Flapping & Conflict Detection)
# --------------------------------------------------
def classify_portal_response(status_code, body_text):
    """
    Analyzes captive portal response to distinguish between:
    - Session limits / phone conflicts
    - Bad credentials
    - Quota exhaustion
    - Redirect or success
    """
    if status_code in (301, 302, 303, 307):
        return LoginOutcome.SUCCESS_OR_REDIRECT, "Redirected after authentication"

    text = (body_text or "").lower()

    # Concurrent session limit keywords (common in pfSense / FreeRADIUS)
    conflict_keywords = [
        "maximum session limit",
        "already logged in",
        "session limit reached",
        "concurrent user login",
        "another ip",
        "simultaneous use",
        "multiple sessions"
    ]
    for kw in conflict_keywords:
        if kw in text:
            return LoginOutcome.SESSION_CONFLICT, f"Concurrent session conflict detected ({kw})"

    # Bad credentials
    auth_fail_keywords = [
        "invalid username or password",
        "invalid credentials",
        "authentication failed",
        "logon failure"
    ]
    for kw in auth_fail_keywords:
        if kw in text:
            return LoginOutcome.AUTH_FAILED, f"Authentication credentials rejected ({kw})"

    # Quota / Traffic limits
    quota_keywords = [
        "quota has been exceeded",
        "data transfer quota",
        "bandwidth limit exceeded",
        "quota reached"
    ]
    for kw in quota_keywords:
        if kw in text:
            return LoginOutcome.QUOTA_EXCEEDED, f"Network quota reached ({kw})"

    if status_code == 200:
        return LoginOutcome.CAPTIVE_UNRESOLVED, "Portal returned 200 OK without explicit confirmation"

    return LoginOutcome.ERROR, f"Portal returned unexpected status HTTP {status_code}"


# --------------------------------------------------
# State Manager (Top Bar Menu Bar Synchronization)
# --------------------------------------------------
class StateManager:
    """
    Manages persistent state in ~/.config/pfsense-autologin/state.json.
    Enables macOS Menu Bar toggle and records live status, standby, and SSID.
    """
    def __init__(self, state_path=None):
        self.state_path = state_path or DEFAULT_STATE_FILE
        self._ensure_dir()

    def _ensure_dir(self):
        directory = os.path.dirname(self.state_path)
        if directory and not os.path.exists(directory):
            try:
                os.makedirs(directory, exist_ok=True)
            except OSError:
                pass

    def load(self):
        default_state = {
            "enabled": True,
            "status": Status.INITIALIZING,
            "status_message": "Ready",
            "last_checked": 0,
            "standby_until": 0,
            "standby_reason": "",
            "current_ssid": None,
            "portal_url": ""
        }
        if not os.path.exists(self.state_path):
            return default_state
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                default_state.update(data)
                return default_state
        except Exception:
            return default_state

    def save(self, updates):
        current = self.load()
        current.update(updates)
        current["last_updated"] = time.time()
        self._ensure_dir()
        temp_file = f"{self.state_path}.tmp.{os.getpid()}"
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(current, f, indent=2)
            os.replace(temp_file, self.state_path)
        except Exception as e:
            logging.error(f"Failed to save state to {self.state_path}: {e}")
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except OSError:
                    pass

    def is_enabled(self):
        return bool(self.load().get("enabled", True))

    def set_enabled(self, enabled):
        self.save({
            "enabled": bool(enabled),
            "status": Status.INITIALIZING if enabled else Status.DISABLED,
            "status_message": "Enabled via Menu Bar" if enabled else "Disabled via Menu Bar"
        })

    def enter_standby(self, cooldown_seconds, reason="Session conflict"):
        standby_until = time.time() + cooldown_seconds
        self.save({
            "status": Status.CONFLICT_STANDBY,
            "status_message": f"Standby: {reason} (cooldown: {int(cooldown_seconds)}s)",
            "standby_until": standby_until,
            "standby_reason": reason
        })

    def is_in_standby(self):
        state = self.load()
        until = state.get("standby_until", 0)
        return time.time() < until

    def get_standby_remaining(self):
        state = self.load()
        remaining = state.get("standby_until", 0) - time.time()
        return max(0, int(remaining))

    def clear_standby(self):
        self.save({
            "standby_until": 0,
            "standby_reason": "",
            "status": Status.INITIALIZING,
            "status_message": "Standby cleared"
        })


# --------------------------------------------------
# Single-Instance Lock
# --------------------------------------------------
class SingleInstanceLock:
    """
    Prevents duplicate instances from running concurrently using fcntl.flock.
    Avoids race conditions and credential spam.
    """
    def __init__(self, lock_path=None):
        self.lock_path = lock_path or DEFAULT_LOCK_FILE
        self.fd = None

    def acquire(self):
        try:
            self.fd = open(self.lock_path, "a+")
            fcntl.flock(self.fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.fd.seek(0)
            self.fd.truncate()
            self.fd.write(f"{os.getpid()}\n")
            self.fd.flush()
            return True
        except (BlockingIOError, IOError):
            if self.fd:
                try:
                    self.fd.close()
                except OSError:
                    pass
                self.fd = None
            return False

    def release(self):
        if self.fd:
            try:
                fcntl.flock(self.fd.fileno(), fcntl.LOCK_UN)
                self.fd.close()
            except OSError:
                pass
            self.fd = None


# --------------------------------------------------
# macOS Network Utilities
# --------------------------------------------------
def wifi_interface():
    try:
        out = subprocess.check_output(
            ["networksetup", "-listallhardwareports"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5
        )
        lines = out.splitlines()
        for i in range(len(lines)):
            if lines[i].strip() == "Hardware Port: Wi-Fi":
                return lines[i + 1].split(": ")[1].strip()
    except Exception:
        pass
    return None


def current_ssid():
    iface = wifi_interface()
    if not iface:
        return None
    try:
        out = subprocess.check_output(
            ["networksetup", "-getairportnetwork", iface],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5
        )
        if "Current Wi-Fi Network:" in out:
            return out.split(": ", 1)[1].strip()
    except Exception:
        pass
    return None


def internet_up(check_url, timeout=5):
    try:
        r = requests.get(check_url, timeout=timeout, allow_redirects=False)
        return r.status_code == 204
    except requests.RequestException:
        return False


def portal_available(url, timeout=5):
    try:
        r = requests.get(url, timeout=timeout, verify=False, allow_redirects=True)
        return r.status_code in (200, 301, 302, 307)
    except requests.RequestException:
        return False


# --------------------------------------------------
# AutoLogin Engine
# --------------------------------------------------
class AutoLoginEngine:
    """
    Main engine orchestrating network state evaluation, session arbitration,
    CSRF-aware authentication, and session release.
    """
    def __init__(self, config, state_mgr=None, lock_file=None):
        self.config = config
        self.state_mgr = state_mgr or StateManager()
        self.lock = SingleInstanceLock(lock_file)
        self.session = requests.Session()

        creds = config["credentials"]
        settings = config["settings"]

        self.username = creds["username"]
        self.password = creds["password"]
        self.portal_url = settings["portal_url"]
        self.check_url = settings.get("connectivity_check_url", "http://clients3.google.com/generate_204")
        self.ssid_required = settings.get("ssid", "").strip()
        self.retry_delay = int(settings.get("retry_delay", "5"))
        self.conflict_cooldown = int(settings.get("conflict_cooldown", "900"))  # Default 15 min

    def attempt_login(self, force=False):
        """
        Extracts portal form, parses CSRF tokens, submits login,
        and analyzes response to prevent session thrashing.
        """
        self.state_mgr.save({
            "status": Status.AUTHENTICATING,
            "status_message": "Connecting to captive portal..."
        })
        logging.info(f"Connecting to captive portal at {self.portal_url}")

        action_url = self.portal_url
        hidden_fields = {}

        # 1. Probe portal page to extract form action and CSRF token
        try:
            resp = self.session.get(self.portal_url, timeout=7, verify=False, allow_redirects=True)
            if resp.status_code == 200:
                url = str(resp.url) if (hasattr(resp, "url") and isinstance(resp.url, str)) else self.portal_url
                parser = PortalFormParser(base_url=url)
                parser.feed(resp.text)
                action_url = parser.action_url
                hidden_fields = parser.hidden_fields
                logging.debug(f"Discovered action URL: {action_url}, hidden fields: {list(hidden_fields.keys())}")
        except requests.RequestException as e:
            logging.warning(f"Could not pre-fetch portal form: {e}. Falling back to default URL.")

        # 2. Build payload with hidden fields, credentials, and submit action
        payload = {}
        payload.update(hidden_fields)
        payload.update({
            "auth_user": self.username,
            "auth_pass": self.password,
            "redirurl": "https://www.google.com",
            "accept": "Login"
        })

        headers = {
            "User-Agent": "pfSense captive portal autologin (macOS)",
            "Referer": self.portal_url
        }

        # 3. Submit login request
        try:
            logging.info("Submitting authentication POST")
            login_resp = self.session.post(
                action_url,
                data=payload,
                headers=headers,
                timeout=10,
                verify=False,
                allow_redirects=False
            )
        except requests.RequestException as e:
            logging.error(f"Login POST request failed: {e}")
            self.state_mgr.save({
                "status": Status.DISCONNECTED,
                "status_message": f"Connection error: {e}"
            })
            return False

        # 4. Classify outcome
        outcome, message = classify_portal_response(login_resp.status_code, login_resp.text)
        logging.info(f"Portal response evaluated: {outcome.value} ({message})")

        if outcome == LoginOutcome.SESSION_CONFLICT:
            logging.warning("=== CONCURRENT SESSION LIMIT REACHED ===")
            logging.warning("Another device (such as your smartphone) is currently using this login slot.")
            logging.warning(f"Entering Standby Mode for {self.conflict_cooldown}s to prevent session thrashing.")
            self.state_mgr.enter_standby(self.conflict_cooldown, reason="Active session on another device")
            return False

        elif outcome == LoginOutcome.AUTH_FAILED:
            logging.error(f"Authentication failed: {message}")
            self.state_mgr.save({
                "status": Status.AUTH_FAILED,
                "status_message": message
            })
            return False

        elif outcome == LoginOutcome.QUOTA_EXCEEDED:
            logging.error(f"Quota exceeded: {message}")
            self.state_mgr.save({
                "status": Status.QUOTA_EXCEEDED,
                "status_message": message
            })
            return False

        # 5. Confirm internet access via 204 check
        time.sleep(1.5)
        if internet_up(self.check_url, timeout=5):
            logging.info("Login successful: Internet access confirmed.")
            self.state_mgr.clear_standby()
            self.state_mgr.save({
                "status": Status.CONNECTED,
                "status_message": "Internet connected"
            })
            return True
        else:
            logging.warning("Portal accepted request but internet 204 check failed.")
            self.state_mgr.save({
                "status": Status.CAPTIVE,
                "status_message": "Still captive after login"
            })
            return False

    def logout(self):
        """
        Releases the pfSense captive portal session so other devices
        (like phone) can connect without conflict.
        Also enters Standby Mode so background automation does not
        immediately re-authenticate and kick the other device off.
        """
        logging.info("Attempting to release (logout) captive portal session")
        parsed = urlparse(self.portal_url)
        # pfSense standard logout endpoint
        logout_url = f"{parsed.scheme}://{parsed.netloc}/index.php?zone={self._extract_zone()}&logout=true"
        try:
            r = self.session.get(logout_url, timeout=5, verify=False)
            logging.info(f"Logout request sent to {logout_url} (HTTP {r.status_code})")
            # Enter standby so background checks don't immediately re-authenticate
            self.state_mgr.enter_standby(self.conflict_cooldown, reason="Session released for phone")
            self.state_mgr.save({
                "status": Status.DISCONNECTED,
                "status_message": "Session released for phone (standby active)"
            })
            return True
        except requests.RequestException as e:
            logging.error(f"Failed to send logout request: {e}")
            return False

    def _extract_zone(self):
        try:
            parsed = urlparse(self.portal_url)
            for part in parsed.query.split("&"):
                if part.startswith("zone="):
                    return part.split("=")[1]
        except Exception:
            pass
        return "hostelzone"

    def step_once(self, force=False):
        """
        Executes a single evaluation step.
        Returns True if network is connected, False otherwise.
        """
        # 1. Check Menu Bar toggle state
        if not self.state_mgr.is_enabled():
            logging.info("Auto-login is disabled via Menu Bar toggle. Exiting.")
            self.state_mgr.save({
                "status": Status.DISABLED,
                "status_message": "Disabled via Menu Bar"
            })
            return False

        # 2. Check SSID guard
        ssid = current_ssid()
        self.state_mgr.save({"current_ssid": ssid})

        if self.ssid_required and ssid:
            if ssid != self.ssid_required:
                logging.info(f"Current SSID '{ssid}' does not match required '{self.ssid_required}'. Standing down.")
                self.state_mgr.save({
                    "status": Status.UNKNOWN_NETWORK,
                    "status_message": f"Connected to {ssid} (target: {self.ssid_required})"
                })
                return False

        # 3. Check if internet is already UP
        if internet_up(self.check_url):
            logging.info("Internet is UP and fully accessible.")
            self.state_mgr.save({
                "status": Status.CONNECTED,
                "status_message": f"Connected ({ssid or 'Wi-Fi'})"
            })
            return True

        logging.warning("Internet is DOWN or captive.")

        # 4. Check if currently in Standby Mode due to phone session conflict
        if self.state_mgr.is_in_standby() and not force:
            remaining = self.state_mgr.get_standby_remaining()
            logging.info(f"In Standby Mode (phone active). Skipping login attempt. Remaining: {remaining}s")
            self.state_mgr.save({
                "status": Status.CONFLICT_STANDBY,
                "status_message": f"Standby: Phone Active ({remaining}s left)"
            })
            return False

        if force:
            logging.info("Force flag specified: bypassing Standby Mode.")
            self.state_mgr.clear_standby()

        # 5. Check if portal is reachable
        if not portal_available(self.portal_url):
            logging.warning(f"Captive portal at {self.portal_url} is not reachable.")
            self.state_mgr.save({
                "status": Status.DISCONNECTED,
                "status_message": "Portal unreachable"
            })
            return False

        # 6. Attempt authentication
        return self.attempt_login(force=force)


# --------------------------------------------------
# Config Loader
# --------------------------------------------------
def load_config(config_path=None):
    path = config_path or DEFAULT_CONFIG_FILE
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Configuration file not found at {path}. "
            "Please copy config.ini.example to config.ini and set your credentials."
        )

    cfg = configparser.ConfigParser()
    cfg.read(path)
    return cfg


# --------------------------------------------------
# CLI Entry Point
# --------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="pfSense Captive Portal Auto-Login with Session Arbitration")
    parser.add_argument("--once", action="store_true", help="Run a single evaluation step and exit")
    parser.add_argument("--force", action="store_true", help="Force login, clearing any active conflict standby")
    parser.add_argument("--logout", action="store_true", help="Release portal session so other devices can connect")
    parser.add_argument("--toggle-on", action="store_true", help="Enable auto-login via state manager")
    parser.add_argument("--toggle-off", action="store_true", help="Disable auto-login via state manager")
    parser.add_argument("--status-json", action="store_true", help="Print current status JSON and exit")
    parser.add_argument("--daemon", action="store_true", help="Run continuously in background daemon loop")
    parser.add_argument("--config", type=str, default=DEFAULT_CONFIG_FILE, help="Path to config.ini")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose debug logging")

    args = parser.parse_args()
    setup_logging(verbose=args.verbose)

    state_mgr = StateManager()

    # Fast handlers for Menu Bar IPC
    if args.toggle_on:
        state_mgr.set_enabled(True)
        print("Auto-login enabled.")
        return

    if args.toggle_off:
        state_mgr.set_enabled(False)
        print("Auto-login disabled.")
        return

    if args.status_json:
        print(json.dumps(state_mgr.load(), indent=2))
        return

    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        logging.error(str(e))
        state_mgr.save({
            "status": Status.AUTH_FAILED,
            "status_message": "config.ini missing"
        })
        sys.exit(1)

    # Acquire mutual exclusion lock
    lock = SingleInstanceLock()
    if not lock.acquire():
        logging.info("Another autologin instance is already running. Exiting cleanly.")
        return

    try:
        engine = AutoLoginEngine(config, state_mgr=state_mgr)

        if args.logout:
            engine.logout()
            return

        if args.once or not args.daemon:
            engine.step_once(force=args.force)
            return

        # Daemon loop mode
        base_delay = int(config["settings"].get("retry_delay", "5"))
        max_delay = 300
        delay = base_delay

        logging.info("Starting persistent daemon loop...")
        while True:
            success = engine.step_once(force=args.force)
            if success:
                delay = base_delay
                time.sleep(30)
            else:
                logging.info(f"Next evaluation in {delay} seconds")
                time.sleep(delay)
                delay = min(delay * 2, max_delay)

    except KeyboardInterrupt:
        logging.info("Stopped by user.")
    except Exception as e:
        logging.critical(f"Fatal error: {e}", exc_info=True)
        state_mgr.save({
            "status": Status.AUTH_FAILED,
            "status_message": f"Fatal error: {e}"
        })
    finally:
        lock.release()


if __name__ == "__main__":
    main()
