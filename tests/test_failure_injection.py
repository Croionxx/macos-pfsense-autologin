import unittest
import os
import json
import tempfile
import time
import signal
from unittest.mock import patch, MagicMock

import portal_autologin as pal

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def read_fixture(name):
    path = os.path.join(FIXTURES_DIR, name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class TestFailureInjectionMatrix(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.temp_dir.name, "state.json")
        self.pid_file = os.path.join(self.temp_dir.name, "worker.pid")
        self.lock_file = os.path.join(self.temp_dir.name, "worker.lock")

        self.pid_mgr = pal.PIDManager(self.pid_file)
        self.state_mgr = pal.StateManager(self.state_file, pid_manager=self.pid_mgr)

        self.config = {
            "credentials": {"username": "hostel_user", "password": "hostel_password"},
            "settings": {
                "portal_url": "http://172.20.28.1:8002/index.php?zone=hostelzone",
                "connectivity_check_url": "http://clients3.google.com/generate_204",
                "retry_delay": "5",
                "ssid": "MBH2_WiFi",
                "conflict_cooldown": "300",
                "max_auth_retries": "3"
            }
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    # ----------------------------------------------------------------------
    # 1. Phone vs Mac Session Conflict & Anti-Flapping
    # ----------------------------------------------------------------------
    @patch("portal_autologin.current_ssid", return_value="MBH2_WiFi")
    @patch("portal_autologin.internet_up", return_value=False)
    @patch("portal_autologin.portal_available", return_value=True)
    def test_phone_mac_session_conflict_anti_flapping(self, mock_avail, mock_up, mock_ssid):
        """
        Scenario: Mac attempts login while phone holds active session.
        Portal returns conflict HTML. Mac MUST enter Standby and refuse
        to spam re-logins to prevent phone disconnect flapping.
        """
        engine = pal.AutoLoginEngine(self.config, state_mgr=self.state_mgr, lock_file=self.lock_file)
        conflict_html = read_fixture("pfsense_conflict.html")

        mock_session = MagicMock()
        mock_get = MagicMock(status_code=200, text=read_fixture("pfsense_login.html"), url="http://172.20.28.1:8002/index.php?zone=hostelzone")
        mock_post = MagicMock(status_code=200, text=conflict_html)
        mock_session.get.return_value = mock_get
        mock_session.post.return_value = mock_post
        engine.session = mock_session

        # 1. First step triggers login and detects conflict
        res = engine.step_once(force=False)
        self.assertFalse(res)
        self.assertTrue(self.state_mgr.is_in_standby())
        self.assertEqual(self.state_mgr.load().get("status"), pal.Status.CONFLICT_STANDBY)

        # 2. Immediate second step (e.g. launchd tick 60s later)
        # MUST NOT send any GET or POST to the portal during standby
        mock_session.get.reset_mock()
        mock_session.post.reset_mock()

        res2 = engine.step_once(force=False)
        self.assertFalse(res2)
        mock_session.get.assert_not_called()
        mock_session.post.assert_not_called()
        self.assertTrue(self.state_mgr.is_in_standby())

    # ----------------------------------------------------------------------
    # 2. Take Over Session with --force Bypasses Standby
    # ----------------------------------------------------------------------
    @patch("portal_autologin.current_ssid", return_value="MBH2_WiFi")
    @patch("portal_autologin.internet_up", side_effect=[False, True])
    @patch("portal_autologin.portal_available", return_value=True)
    def test_takeover_session_bypasses_standby_with_force(self, mock_avail, mock_up, mock_ssid):
        """
        Scenario: User explicitly clicks 'Take Over Session' or passes --force.
        Engine MUST clear standby and successfully authenticate.
        """
        self.state_mgr.enter_standby(cooldown_seconds=300, reason="Phone active")
        self.assertTrue(self.state_mgr.is_in_standby())

        engine = pal.AutoLoginEngine(self.config, state_mgr=self.state_mgr, lock_file=self.lock_file)
        mock_session = MagicMock()
        mock_session.get.return_value = MagicMock(status_code=200, text=read_fixture("pfsense_login.html"), url="http://172.20.28.1:8002/index.php?zone=hostelzone")
        mock_session.post.return_value = MagicMock(status_code=302, text="Redirecting")
        engine.session = mock_session

        res = engine.step_once(force=True)
        self.assertTrue(res)
        self.assertFalse(self.state_mgr.is_in_standby())
        self.assertEqual(self.state_mgr.load().get("status"), pal.Status.CONNECTED)

    # ----------------------------------------------------------------------
    # 3. Bad Credentials & Lockout Protection
    # ----------------------------------------------------------------------
    @patch("portal_autologin.current_ssid", return_value="MBH2_WiFi")
    @patch("portal_autologin.internet_up", return_value=False)
    @patch("portal_autologin.portal_available", return_value=True)
    def test_bad_credentials_lockout_protection(self, mock_avail, mock_up, mock_ssid):
        """
        Scenario: Repeated incorrect passwords.
        Engine MUST increment failure count and block further authentication
        attempts once max_auth_retries is reached, protecting against FreeRADIUS account bans.
        """
        engine = pal.AutoLoginEngine(self.config, state_mgr=self.state_mgr, lock_file=self.lock_file)
        auth_fail_html = read_fixture("pfsense_auth_fail.html")

        mock_session = MagicMock()
        mock_session.get.return_value = MagicMock(status_code=200, text=read_fixture("pfsense_login.html"), url="http://172.20.28.1:8002/index.php?zone=hostelzone")
        mock_session.post.return_value = MagicMock(status_code=200, text=auth_fail_html)
        engine.session = mock_session

        # Fail 1
        engine.step_once()
        self.assertEqual(self.state_mgr.load().get("auth_failure_count"), 1)

        # Fail 2
        engine.step_once()
        self.assertEqual(self.state_mgr.load().get("auth_failure_count"), 2)

        # Fail 3 -> Reaches limit
        engine.step_once()
        self.assertEqual(self.state_mgr.load().get("auth_failure_count"), 3)
        self.assertEqual(self.state_mgr.load().get("status"), pal.Status.AUTH_FAILED)

        # 4th attempt: MUST BE BLOCKED BEFORE FIRING POST REQUESTS
        mock_session.post.reset_mock()
        blocked_result = engine.attempt_login(force=False)
        self.assertFalse(blocked_result)
        mock_session.post.assert_not_called()

    # ----------------------------------------------------------------------
    # 4. Sleep / Wake Transient Network Degradation
    # ----------------------------------------------------------------------
    @patch("portal_autologin.current_ssid", return_value="MBH2_WiFi")
    @patch("portal_autologin.internet_up", return_value=False)
    def test_sleep_wake_transient_network_degradation(self, mock_up, mock_ssid):
        """
        Scenario: MacBook wakes from sleep. Wi-Fi interface reports UP, but
        gateway is unreachable (DNS or socket timeout). State MUST transition
        to DEGRADED, not misclassify as AUTH_FAILED or SESSION_CONFLICT.
        """
        engine = pal.AutoLoginEngine(self.config, state_mgr=self.state_mgr, lock_file=self.lock_file)

        # Portal is unreachable during DHCP / routing sync
        with patch("portal_autologin.portal_available", return_value=False):
            res = engine.step_once()
            self.assertFalse(res)
            self.assertEqual(self.state_mgr.load().get("status"), pal.Status.DEGRADED)
            self.assertIn("Portal unreachable", self.state_mgr.load().get("status_message", ""))

    # ----------------------------------------------------------------------
    # 5. Expired CSRF Token Transparent Retry
    # ----------------------------------------------------------------------
    def test_expired_csrf_token_transparent_retry(self):
        """
        Scenario: Initial POST returns 403 / CSRF mismatch.
        Engine MUST detect expired token, re-fetch fresh form, and retry once.
        """
        engine = pal.AutoLoginEngine(self.config, state_mgr=self.state_mgr, lock_file=self.lock_file)
        login_html = read_fixture("pfsense_login.html")

        mock_session = MagicMock()
        # First GET (stale form)
        mock_session.get.side_effect = [
            MagicMock(status_code=200, text=login_html, url="http://172.20.28.1:8002"),
            MagicMock(status_code=200, text=login_html, url="http://172.20.28.1:8002") # Fresh form on retry
        ]
        # First POST fails with 403 CSRF error, second POST succeeds with 302
        mock_session.post.side_effect = [
            MagicMock(status_code=403, text="CSRF token mismatch"),
            MagicMock(status_code=302, text="Redirecting...")
        ]
        engine.session = mock_session

        with patch("portal_autologin.internet_up", return_value=True):
            res = engine.attempt_login()
            self.assertTrue(res)
            self.assertEqual(mock_session.post.call_count, 2)
            self.assertEqual(self.state_mgr.load().get("status"), pal.Status.CONNECTED)

    # ----------------------------------------------------------------------
    # 6. Active In-Flight Process Cancellation on Toggle OFF
    # ----------------------------------------------------------------------
    def test_active_inflight_cancellation(self):
        """
        Scenario: Worker is actively running. User toggles OFF.
        PIDManager MUST send SIGTERM to the recorded worker PID.
        """
        with patch("os.kill") as mock_kill, patch("os.path.exists", return_value=True):
            with patch("builtins.open", unittest.mock.mock_open(read_data="99999\n")):
                cancelled = self.pid_mgr.cancel_active_worker()
                self.assertTrue(cancelled)
                mock_kill.assert_any_call(99999, signal.SIGTERM)

    # ----------------------------------------------------------------------
    # 7. Authoritative State Machine Transition Validation
    # ----------------------------------------------------------------------
    def test_state_machine_transition_enforcement(self):
        """
        Scenario: Ensure invalid transitions are strictly rejected.
        """
        sm = pal.StateMachine(self.state_mgr)
        self.state_mgr.save({"status": pal.Status.CONNECTED})

        # Connected cannot jump directly to Standby without being captive/authenticating
        illegal_transition = sm.transition_to(pal.Status.STANDBY)
        self.assertFalse(illegal_transition)
        self.assertEqual(sm.current_status, pal.Status.CONNECTED)

        # Valid transition: Connected -> Captive
        valid_transition = sm.transition_to(pal.Status.CAPTIVE)
        self.assertTrue(valid_transition)
        self.assertEqual(sm.current_status, pal.Status.CAPTIVE)


if __name__ == "__main__":
    unittest.main()
