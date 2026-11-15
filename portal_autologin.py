#!/usr/bin/env python3
"""
pfSense Captive Portal Auto-Login with Authoritative State Machine,
Anti-Flapping Session Arbitration, and macOS Menu Bar Toggle Controller.
"""

import os
import sys
import time
import fcntl
import json
import signal
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
DEFAULT_PID_FILE = os.path.join(STATE_DIR, "worker.pid")


# --------------------------------------------------
# States and Outcomes
# --------------------------------------------------
class Status:
    INITIALIZING = "INITIALIZING"
    CONNECTED = "CONNECTED"
    CAPTIVE = "CAPTIVE"
    AUTHENTICATING = "AUTHENTICATING"
    STANDBY = "STANDBY"
    CONFLICT_STANDBY = "CONFLICT_STANDBY"  # Backward-compatible alias
    AUTH_FAILED = "AUTH_FAILED"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"      # Backward-compatible alias
    DEGRADED = "DEGRADED"
    DISCONNECTED = "DISCONNECTED"          # Backward-compatible alias
    DISABLED = "DISABLED"
    UNKNOWN_NETWORK = "UNKNOWN_NETWORK"


class LoginOutcome(Enum):
    SUCCESS_OR_REDIRECT = "SUCCESS_OR_REDIRECT"
    SESSION_CONFLICT = "SESSION_CONFLICT"
    AUTH_FAILED = "AUTH_FAILED"
    CSRF_OR_EXPIRED = "CSRF_OR_EXPIRED"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
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
    and all hidden input fields (such as __csrf_magic, zone, redirurl).
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
                if input_type == "hidden" and name.lower() not in ("logout_id", "logout"):
                    self.hidden_fields[name] = value

    def handle_endtag(self, tag):
        if tag.lower() == "form":
            self.in_form = False


# --------------------------------------------------
# Response Classification
# --------------------------------------------------
def classify_portal_response(status_code, body_text):
    """
    Analyzes captive portal response to distinguish between:
    - Session conflicts / phone active
    - Bad credentials (password error)
    - CSRF token expiration
    - Quota exhaustion
    - Redirect or success
    """
    if status_code in (301, 302, 303, 307):
        return LoginOutcome.SUCCESS_OR_REDIRECT, "Redirected after authentication"

    text = (body_text or "").lower()

    # 1. Concurrent session limit keywords (phone conflict)
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
            return LoginOutcome.SESSION_CONFLICT, f"Concurrent session conflict ({kw})"

    # 2. Bad credentials
    auth_fail_keywords = [
        "invalid username or password",
        "invalid credentials",
        "authentication failed",
        "logon failure"
    ]
    for kw in auth_fail_keywords:
        if kw in text:
            return LoginOutcome.AUTH_FAILED, f"Authentication credentials rejected ({kw})"

    # 3. Quota / Traffic limits
    quota_keywords = [
        "quota has been exceeded",
        "data transfer quota",
        "bandwidth limit exceeded",
        "quota reached"
    ]
    for kw in quota_keywords:
        if kw in text:
            return LoginOutcome.QUOTA_EXHAUSTED, f"Network quota reached ({kw})"

    # 4. CSRF / Token error
    if "csrf" in text or status_code == 403:
        return LoginOutcome.CSRF_OR_EXPIRED, "CSRF token mismatch or expired challenge"

    if status_code == 200:
        return LoginOutcome.CAPTIVE_UNRESOLVED, "Portal returned 200 OK without explicit confirmation"

    return LoginOutcome.ERROR, f"Portal returned unexpected status HTTP {status_code}"


# --------------------------------------------------
# Process Lifetime & In-Flight Cancellation
# --------------------------------------------------
class PIDManager:
    """
    Tracks active worker PID to support immediate cancellation
    of in-flight HTTP requests and sleeps when toggled OFF.
    """
    def __init__(self, pid_file=None):
        self.pid_file = pid_file or DEFAULT_PID_FILE

    def record_pid(self):
        directory = os.path.dirname(self.pid_file)
        if directory and not os.path.exists(directory):
            try:
                os.makedirs(directory, exist_ok=True)
            except OSError:
                pass
        try:
            with open(self.pid_file, "w") as f:
                f.write(f"{os.getpid()}\n")
        except OSError:
            pass

    def clear_pid(self):
        try:
            if os.path.exists(self.pid_file):
                os.remove(self.pid_file)
        except OSError:
            pass

    def cancel_active_worker(self):
        """Sends SIGTERM to cancel any running in-flight worker."""
        if not os.path.exists(self.pid_file):
            return False
        try:
            with open(self.pid_file, "r") as f:
                content = f.read().strip()
            if not content.isdigit():
                return False
            pid = int(content)
            if pid != os.getpid():
                # Check if process is alive
                os.kill(pid, 0)
                logging.info(f"Terminating active in-flight worker (PID {pid})")
                os.kill(pid, signal.SIGTERM)
                return True
        except (OSError, ValueError):
            pass
        return False


# --------------------------------------------------
# State Manager (Top Bar Menu Bar Synchronization)
# --------------------------------------------------
class StateManager:
    """
    Manages persistent state in ~/.config/pfsense-autologin/state.json.
    Enables macOS Menu Bar toggle and records live status, standby, and SSID.
    """
    def __init__(self, state_path=None, pid_manager=None):
        self.state_path = state_path or DEFAULT_STATE_FILE
        self.pid_manager = pid_manager or PIDManager()
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
            "portal_url": "",
            "auth_failure_count": 0
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
        if not enabled:
            # Active in-flight request cancellation guarantee
            self.pid_manager.cancel_active_worker()

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

    def record_auth_failure(self):
        count = self.load().get("auth_failure_count", 0) + 1
        self.save({"auth_failure_count": count})
        return count

    def reset_auth_failures(self):
        self.save({"auth_failure_count": 0})


# --------------------------------------------------
# Authoritative State Machine Controller
# --------------------------------------------------
class StateMachine:
    """
    Authoritative state controller enforcing explicit valid transitions
    and preventing rogue state mutations.
    """
    VALID_TRANSITIONS = {
        Status.INITIALIZING: {
            Status.CONNECTED, Status.CAPTIVE, Status.AUTHENTICATING,
            Status.DEGRADED, Status.DISCONNECTED, Status.DISABLED,
            Status.INITIALIZING, Status.AUTH_FAILED, Status.UNKNOWN_NETWORK
        },
        Status.CONNECTED: {
            Status.CAPTIVE, Status.DEGRADED, Status.DISCONNECTED,
            Status.DISABLED, Status.CONNECTED, Status.AUTHENTICATING
        },
        Status.CAPTIVE: {
            Status.AUTHENTICATING, Status.DEGRADED, Status.DISCONNECTED,
            Status.DISABLED, Status.CAPTIVE, Status.CONFLICT_STANDBY,
            Status.STANDBY
        },
        Status.AUTHENTICATING: {
            Status.CONNECTED, Status.STANDBY, Status.CONFLICT_STANDBY,
            Status.AUTH_FAILED, Status.QUOTA_EXHAUSTED, Status.QUOTA_EXCEEDED,
            Status.DEGRADED, Status.DISCONNECTED, Status.DISABLED, Status.CAPTIVE
        },
        Status.STANDBY: {
            Status.AUTHENTICATING, Status.CONNECTED, Status.DISABLED,
            Status.STANDBY, Status.INITIALIZING, Status.CONFLICT_STANDBY,
            Status.CAPTIVE, Status.DEGRADED
        },
        Status.CONFLICT_STANDBY: {
            Status.AUTHENTICATING, Status.CONNECTED, Status.DISABLED,
            Status.CONFLICT_STANDBY, Status.INITIALIZING, Status.STANDBY,
            Status.CAPTIVE, Status.DEGRADED
        },
        Status.AUTH_FAILED: {
            Status.AUTHENTICATING, Status.DISABLED, Status.INITIALIZING,
            Status.AUTH_FAILED, Status.CAPTIVE
        },
        Status.QUOTA_EXHAUSTED: {
            Status.AUTHENTICATING, Status.DISABLED, Status.INITIALIZING,
            Status.QUOTA_EXHAUSTED, Status.CAPTIVE
        },
        Status.QUOTA_EXCEEDED: {
            Status.AUTHENTICATING, Status.DISABLED, Status.INITIALIZING,
            Status.QUOTA_EXCEEDED, Status.CAPTIVE
        },
        Status.DEGRADED: {
            Status.CONNECTED, Status.CAPTIVE, Status.DISABLED,
            Status.DEGRADED, Status.DISCONNECTED, Status.AUTHENTICATING
        },
        Status.DISCONNECTED: {
            Status.CONNECTED, Status.CAPTIVE, Status.DISABLED,
            Status.DEGRADED, Status.DISCONNECTED, Status.INITIALIZING,
            Status.AUTHENTICATING
        },
        Status.DISABLED: {
            Status.INITIALIZING, Status.CONNECTED, Status.CAPTIVE, Status.DISABLED
        },
        Status.UNKNOWN_NETWORK: {
            Status.CONNECTED, Status.CAPTIVE, Status.DISABLED, Status.UNKNOWN_NETWORK
        }
    }

    def __init__(self, state_mgr):
        self.state_mgr = state_mgr

    @property
    def current_status(self):
        return self.state_mgr.load().get("status", Status.INITIALIZING)

    def transition_to(self, new_status, message=None, extra=None):
        current = self.current_status
        allowed = self.VALID_TRANSITIONS.get(current, set())
        if new_status not in allowed and current != new_status:
            logging.warning(
                f"Invalid state transition rejected: '{current}' -> '{new_status}'. Allowed: {allowed}"
            )
            return False

        payload = {"status": new_status}
        if message is not None:
            payload["status_message"] = message
        if extra:
            payload.update(extra)

        self.state_mgr.save(payload)
        return True


# --------------------------------------------------
# Single-Instance Lock
# --------------------------------------------------
class SingleInstanceLock:
    """
    Prevents duplicate instances from running concurrently using fcntl.flock.
    Avoids race conditions, inode races, and credential spam.
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
        self.sm = StateMachine(self.state_mgr)
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
        self.max_auth_retries = int(settings.get("max_auth_retries", "3"))

    def attempt_login(self, force=False):
        """
        Extracts portal form, parses CSRF tokens, submits login,
        and analyzes response to prevent session thrashing.
        """
        # Hard lockout guard: Protect against permanent account lockouts
        failure_count = self.state_mgr.load().get("auth_failure_count", 0)
        if failure_count >= self.max_auth_retries and not force:
            logging.error(
                f"Authentication blocked: {failure_count} consecutive credential failures. "
                "Update config.ini or use --force to override."
            )
            self.sm.transition_to(
                Status.AUTH_FAILED,
                message=f"Locked out: {failure_count} bad login attempts"
            )
            return False

        self.sm.transition_to(
            Status.AUTHENTICATING,
            message="Connecting to captive portal..."
        )
        logging.info(f"Connecting to captive portal at {self.portal_url}")

        action_url = self.portal_url
        hidden_fields = {}

        # 1. Probe portal page to extract form action and CSRF token
        try:
            resp = self.session.get(self.portal_url, timeout=7, verify=False, allow_redirects=True)
            if resp.status_code == 200:
                if "portal session is connected" in resp.text.lower():
                    logging.info("Captive portal reports session is already connected.")
                    self.state_mgr.clear_standby()
                    self.state_mgr.reset_auth_failures()
                    self.sm.transition_to(
                        Status.CONNECTED,
                        message="Portal session is connected"
                    )
                    return True

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

        # 3. Submit login request with allow_redirects=False to prevent early connection drops
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
            logging.error(f"Login POST request failed (network transport error): {e}")
            self.sm.transition_to(
                Status.DEGRADED,
                message=f"Transport error: {e}"
            )
            return False

        # 4. Classify outcome
        outcome, message = classify_portal_response(login_resp.status_code, login_resp.text)
        logging.info(f"Portal response evaluated: {outcome.value} ({message})")

        # Handle CSRF token mismatch / expired challenge transparent retry
        if outcome == LoginOutcome.CSRF_OR_EXPIRED:
            logging.info("Detected expired challenge / CSRF token. Fetching fresh form and retrying once...")
            try:
                fresh_resp = self.session.get(self.portal_url, timeout=7, verify=False, allow_redirects=True)
                parser = PortalFormParser(base_url=self.portal_url)
                parser.feed(fresh_resp.text)
                payload.update(parser.hidden_fields)
                login_resp = self.session.post(
                    parser.action_url,
                    data=payload,
                    headers=headers,
                    timeout=10,
                    verify=False,
                    allow_redirects=False
                )
                outcome, message = classify_portal_response(login_resp.status_code, login_resp.text)
            except requests.RequestException:
                pass

        if outcome == LoginOutcome.SESSION_CONFLICT:
            logging.warning("=== CONCURRENT SESSION LIMIT REACHED ===")
            logging.warning("Another device (such as your smartphone) is currently using this login slot.")
            logging.warning(f"Entering Standby Mode for {self.conflict_cooldown}s to prevent session thrashing.")
            self.state_mgr.enter_standby(self.conflict_cooldown, reason="Active session on another device")
            return False

        elif outcome == LoginOutcome.AUTH_FAILED:
            logging.error(f"Authentication failed: {message}")
            self.state_mgr.record_auth_failure()
            self.sm.transition_to(
                Status.AUTH_FAILED,
                message=message
            )
            return False

        elif outcome in (LoginOutcome.QUOTA_EXHAUSTED, LoginOutcome.QUOTA_EXCEEDED):
            logging.error(f"Quota exceeded: {message}")
            self.sm.transition_to(
                Status.QUOTA_EXHAUSTED,
                message=message
            )
            return False

        # 5. Confirm internet access via 204 check
        time.sleep(1.5)
        if internet_up(self.check_url, timeout=5):
            logging.info("Login successful: Internet access confirmed.")
            self.state_mgr.clear_standby()
            self.state_mgr.reset_auth_failures()
            self.sm.transition_to(
                Status.CONNECTED,
                message="Internet connected"
            )
            return True
        else:
            logging.warning("Portal accepted request but internet 204 check failed.")
            self.sm.transition_to(
                Status.CAPTIVE,
                message="Still captive after login"
            )
            return False

    def logout(self):
        """
        Releases the pfSense captive portal session so other devices
        (like phone) can connect without conflict.
        Also enters Standby Mode so background automation does not
        immediately re-authenticate and kick the other device off.
        """
        import re
        logging.info("Attempting to release (logout) captive portal session")
        parsed = urlparse(self.portal_url)
        action_url = self.portal_url
        payload = {"zone": self._extract_zone(), "logout": "Disconnect"}

        try:
            resp = self.session.get(self.portal_url, timeout=5, verify=False)
            if resp.status_code == 200:
                if "logout_id" in resp.text:
                    m_id = re.search(r'name=["\']logout_id["\']\s+type=["\']hidden["\']\s+value=["\']([^"\']+)["\']', resp.text)
                    if not m_id:
                        m_id = re.search(r'value=["\']([^"\']+)["\']\s+name=["\']logout_id["\']', resp.text)
                    if m_id:
                        payload["logout_id"] = m_id.group(1)
                    m_action = re.search(r'<form[^>]+action=["\']([^"\']+)["\']', resp.text)
                    if m_action:
                        action_url = urljoin(self.portal_url, m_action.group(1))

            if "logout_id" in payload:
                r = self.session.post(action_url, data=payload, timeout=5, verify=False, allow_redirects=False)
            else:
                logout_url = f"{parsed.scheme}://{parsed.netloc}/index.php?zone={self._extract_zone()}&logout=true"
                r = self.session.get(logout_url, timeout=5, verify=False)

            logging.info(f"Logout request sent (HTTP {r.status_code})")
            self.state_mgr.enter_standby(self.conflict_cooldown, reason="Session released for phone")
            self.sm.transition_to(
                Status.DEGRADED,
                message="Session released for phone (standby active)"
            )
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
            self.sm.transition_to(
                Status.DISABLED,
                message="Disabled via Menu Bar"
            )
            return False

        # 2. Check SSID guard
        ssid = current_ssid()
        self.state_mgr.save({"current_ssid": ssid})

        if self.ssid_required and ssid:
            if ssid != self.ssid_required:
                logging.info(f"Current SSID '{ssid}' does not match required '{self.ssid_required}'. Standing down.")
                self.sm.transition_to(
                    Status.UNKNOWN_NETWORK,
                    message=f"Connected to {ssid} (target: {self.ssid_required})"
                )
                return False

        # 3. Check if internet is already UP
        if internet_up(self.check_url):
            logging.info("Internet is UP and fully accessible.")
            self.state_mgr.reset_auth_failures()
            self.sm.transition_to(
                Status.CONNECTED,
                message=f"Connected ({ssid or 'Wi-Fi'})"
            )
            return True

        logging.warning("Internet is DOWN or captive.")
        self.sm.transition_to(Status.CAPTIVE, message="Internet captive; evaluating portal")

        # 4. Check if currently in Standby Mode due to phone session conflict
        if self.state_mgr.is_in_standby() and not force:
            remaining = self.state_mgr.get_standby_remaining()
            logging.info(f"In Standby Mode (phone active). Skipping login attempt. Remaining: {remaining}s")
            self.sm.transition_to(
                Status.CONFLICT_STANDBY,
                message=f"Standby: Phone Active ({remaining}s left)"
            )
            return False

        if force:
            logging.info("Force flag specified: bypassing Standby Mode.")
            self.state_mgr.clear_standby()

        # 5. Check if portal is reachable
        if not portal_available(self.portal_url):
            logging.warning(f"Captive portal at {self.portal_url} is not reachable.")
            self.sm.transition_to(
                Status.DEGRADED,
                message="Portal unreachable"
            )
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
    parser = argparse.ArgumentParser(description="pfSense Captive Portal Auto-Login with Authoritative State Machine")
    parser.add_argument("--once", action="store_true", help="Run a single evaluation step and exit")
    parser.add_argument("--force", action="store_true", help="Force login, clearing active phone standby")
    parser.add_argument("--logout", action="store_true", help="Release portal session so other devices can connect")
    parser.add_argument("--toggle-on", action="store_true", help="Enable auto-login via state manager")
    parser.add_argument("--toggle-off", action="store_true", help="Disable auto-login & cancel in-flight worker")
    parser.add_argument("--status-json", action="store_true", help="Print current status JSON and exit")
    parser.add_argument("--reset-auth-lockout", action="store_true", help="Reset consecutive bad password counters")
    parser.add_argument("--daemon", action="store_true", help="Run continuously in background daemon loop")
    parser.add_argument("--config", type=str, default=DEFAULT_CONFIG_FILE, help="Path to config.ini")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose debug logging")

    args = parser.parse_args()
    setup_logging(verbose=args.verbose)

    pid_mgr = PIDManager()
    state_mgr = StateManager(pid_manager=pid_mgr)

    # Fast handlers for Menu Bar IPC
    if args.toggle_on:
        state_mgr.set_enabled(True)
        print("Auto-login enabled.")
        return

    if args.toggle_off:
        state_mgr.set_enabled(False)
        print("Auto-login disabled.")
        return

    if args.reset_auth_lockout:
        state_mgr.reset_auth_failures()
        print("Authentication lockout counter reset.")
        return

    if args.status_json:
        print(json.dumps(state_mgr.load(), indent=2))
        return

    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        logging.error(str(e))
        state_mgr.save({
            "status": Status.DEGRADED,
            "status_message": "config.ini missing"
        })
        sys.exit(1)

    # Acquire mutual exclusion lock
    lock = SingleInstanceLock()
    if not lock.acquire():
        logging.info("Another autologin instance is already running. Exiting cleanly.")
        return

    # Record active PID for in-flight cancellation
    pid_mgr.record_pid()

    # Graceful signal handler
    def handle_signal(signum, frame):
        logging.info(f"Signal {signum} received: cancelling worker.")
        state_mgr.save({
            "status": Status.DISABLED,
            "status_message": "Stopped via signal"
        })
        lock.release()
        pid_mgr.clear_pid()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

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
            "status": Status.DEGRADED,
            "status_message": f"Fatal error: {e}"
        })
    finally:
        pid_mgr.clear_pid()
        lock.release()


if __name__ == "__main__":
    main()
