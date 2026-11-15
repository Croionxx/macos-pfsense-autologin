import unittest
import os
import json
import tempfile
import time
from unittest.mock import patch, MagicMock

# We will import from portal_autologin
import portal_autologin as pal


class TestPortalFormParser(unittest.TestCase):
    def test_extract_form_and_tokens(self):
        sample_html = """
        <!DOCTYPE html>
        <html>
        <head><title>pfSense Captive Portal</title></head>
        <body>
            <form method="post" action="/index.php?zone=hostelzone">
                <input name="__csrf_magic" type="hidden" value="sid:abcdef123456" />
                <input name="zone" type="hidden" value="hostelzone" />
                <input name="redirurl" type="hidden" value="http://example.com" />
                <input name="auth_user" type="text" />
                <input name="auth_pass" type="password" />
                <input name="accept" type="submit" value="Sign In" />
            </form>
        </body>
        </html>
        """
        parser = pal.PortalFormParser(base_url="http://172.20.28.1:8002/index.php?zone=hostelzone")
        parser.feed(sample_html)

        self.assertEqual(parser.action_url, "http://172.20.28.1:8002/index.php?zone=hostelzone")
        self.assertEqual(parser.hidden_fields.get("__csrf_magic"), "sid:abcdef123456")
        self.assertEqual(parser.hidden_fields.get("zone"), "hostelzone")
        self.assertEqual(parser.hidden_fields.get("redirurl"), "http://example.com")

    def test_extract_form_with_relative_and_custom_action(self):
        sample_html = """
        <form method="POST" action="https://172.20.28.1:8003/index.php?zone=guest">
            <input name="csrf_token" type="hidden" value="tok_998877" />
        </form>
        """
        parser = pal.PortalFormParser(base_url="http://172.20.28.1:8002")
        parser.feed(sample_html)
        self.assertEqual(parser.action_url, "https://172.20.28.1:8003/index.php?zone=guest")
        self.assertEqual(parser.hidden_fields.get("csrf_token"), "tok_998877")


class TestSessionResponseDetection(unittest.TestCase):
    def test_detect_conflict(self):
        conflict_samples = [
            "Your maximum session limit has been reached. Please disconnect other devices.",
            "<div class='alert'>User already logged in from 172.20.28.45</div>",
            "Error: Concurrent user logins are disabled on this network.",
            "Session limit reached for user student123",
        ]
        for text in conflict_samples:
            result, reason = pal.classify_portal_response(200, text)
            self.assertEqual(result, pal.LoginOutcome.SESSION_CONFLICT, f"Failed for: {text}")

    def test_detect_auth_failure(self):
        fail_samples = [
            "Invalid username or password",
            "<div class='error'>Invalid credentials provided</div>",
            "Authentication failed for user",
        ]
        for text in fail_samples:
            result, reason = pal.classify_portal_response(200, text)
            self.assertEqual(result, pal.LoginOutcome.AUTH_FAILED, f"Failed for: {text}")

    def test_detect_quota_exceeded(self):
        quota_samples = [
            "Your daily bandwidth quota has been exceeded",
            "Data transfer quota reached",
        ]
        for text in quota_samples:
            result, reason = pal.classify_portal_response(200, text)
            self.assertEqual(result, pal.LoginOutcome.QUOTA_EXCEEDED, f"Failed for: {text}")

    def test_detect_redirect_success(self):
        result, reason = pal.classify_portal_response(302, "")
        self.assertEqual(result, pal.LoginOutcome.SUCCESS_OR_REDIRECT)


class TestStateManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.temp_dir.name, "state.json")
        self.state_mgr = pal.StateManager(self.state_file)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_default_state(self):
        state = self.state_mgr.load()
        self.assertTrue(state.get("enabled", True))
        self.assertEqual(state.get("status"), pal.Status.INITIALIZING)

    def test_toggle_state(self):
        self.state_mgr.set_enabled(False)
        self.assertFalse(self.state_mgr.is_enabled())

        self.state_mgr.set_enabled(True)
        self.assertTrue(self.state_mgr.is_enabled())

    def test_standby_management(self):
        # Setting standby for 60 seconds
        self.state_mgr.enter_standby(cooldown_seconds=60, reason="Phone is logged in")
        self.assertTrue(self.state_mgr.is_in_standby())
        self.assertGreater(self.state_mgr.get_standby_remaining(), 0)

        # Clear standby
        self.state_mgr.clear_standby()
        self.assertFalse(self.state_mgr.is_in_standby())
        self.assertEqual(self.state_mgr.get_standby_remaining(), 0)


class TestSingleInstanceLock(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.lock_file = os.path.join(self.temp_dir.name, "test.lock")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_lock_and_prevent_concurrency(self):
        lock1 = pal.SingleInstanceLock(self.lock_file)
        self.assertTrue(lock1.acquire())

        # Second lock attempt should fail non-blocking
        lock2 = pal.SingleInstanceLock(self.lock_file)
        self.assertFalse(lock2.acquire())

        # Release lock 1
        lock1.release()

        # Now lock 2 should succeed
        self.assertTrue(lock2.acquire())
        lock2.release()


class TestAutoLoginEngine(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.temp_dir.name, "state.json")
        self.lock_file = os.path.join(self.temp_dir.name, "test.lock")

        self.config = {
            "credentials": {"username": "testuser", "password": "testpassword"},
            "settings": {
                "portal_url": "http://172.20.28.1:8002/index.php?zone=hostelzone",
                "connectivity_check_url": "http://clients3.google.com/generate_204",
                "retry_delay": "5",
                "ssid": "MBH2_WiFi",
                "conflict_cooldown": "300",
                "check_interval": "15"
            }
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch("portal_autologin.internet_up")
    def test_skip_when_disabled(self, mock_internet_up):
        state_mgr = pal.StateManager(self.state_file)
        state_mgr.set_enabled(False)

        engine = pal.AutoLoginEngine(self.config, state_mgr=state_mgr, lock_file=self.lock_file)
        executed = engine.step_once()
        self.assertFalse(executed)
        mock_internet_up.assert_not_called()
        self.assertEqual(state_mgr.load().get("status"), pal.Status.DISABLED)

    @patch("portal_autologin.internet_up", return_value=True)
    def test_internet_already_up(self, mock_internet_up):
        state_mgr = pal.StateManager(self.state_file)
        engine = pal.AutoLoginEngine(self.config, state_mgr=state_mgr, lock_file=self.lock_file)
        
        executed = engine.step_once()
        self.assertTrue(executed)
        self.assertEqual(state_mgr.load().get("status"), pal.Status.CONNECTED)

    @patch("portal_autologin.internet_up", return_value=False)
    @patch("portal_autologin.current_ssid", return_value="MBH2_WiFi")
    def test_standby_skips_login_attempt(self, mock_ssid, mock_internet_up):
        state_mgr = pal.StateManager(self.state_file)
        state_mgr.enter_standby(cooldown_seconds=300, reason="Device conflict")

        engine = pal.AutoLoginEngine(self.config, state_mgr=state_mgr, lock_file=self.lock_file)
        with patch.object(engine, "attempt_login") as mock_login:
            engine.step_once()
            mock_login.assert_not_called()
            self.assertEqual(state_mgr.load().get("status"), pal.Status.CONFLICT_STANDBY)

    @patch("portal_autologin.internet_up", return_value=True)
    @patch("portal_autologin.current_ssid", return_value="MBH2_WiFi")
    def test_successful_login_with_form_and_csrf(self, mock_ssid, mock_internet_up):
        state_mgr = pal.StateManager(self.state_file)
        engine = pal.AutoLoginEngine(self.config, state_mgr=state_mgr, lock_file=self.lock_file)

        mock_session = MagicMock()
        # GET portal page
        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.text = """
        <form method="post" action="/index.php?zone=hostelzone">
            <input type="hidden" name="__csrf_magic" value="token123" />
        </form>
        """
        mock_session.get.return_value = mock_get_resp

        # POST credentials
        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 302
        mock_post_resp.text = "Redirecting..."
        mock_session.post.return_value = mock_post_resp

        engine.session = mock_session
        result = engine.attempt_login()
        self.assertTrue(result)
        self.assertEqual(state_mgr.load().get("status"), pal.Status.CONNECTED)

        # Verify POST data included __csrf_magic
        args, kwargs = mock_session.post.call_args
        posted_data = kwargs.get("data", {})
        self.assertEqual(posted_data.get("__csrf_magic"), "token123")
        self.assertEqual(posted_data.get("auth_user"), "testuser")

    @patch("portal_autologin.internet_up", return_value=False)
    @patch("portal_autologin.current_ssid", return_value="MBH2_WiFi")
    def test_session_conflict_triggers_standby(self, mock_ssid, mock_internet_up):
        state_mgr = pal.StateManager(self.state_file)
        engine = pal.AutoLoginEngine(self.config, state_mgr=state_mgr, lock_file=self.lock_file)

        mock_session = MagicMock()
        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.text = "<form action='/index.php?zone=hostelzone'></form>"
        mock_session.get.return_value = mock_get_resp

        # POST returns conflict
        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 200
        mock_post_resp.text = "Your maximum session limit has been reached."
        mock_session.post.return_value = mock_post_resp

        engine.session = mock_session
        result = engine.attempt_login()
        self.assertFalse(result)
        self.assertTrue(state_mgr.is_in_standby())
        self.assertEqual(state_mgr.load().get("status"), pal.Status.CONFLICT_STANDBY)

    def test_logout_portal(self):
        state_mgr = pal.StateManager(self.state_file)
        engine = pal.AutoLoginEngine(self.config, state_mgr=state_mgr, lock_file=self.lock_file)

        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_session.get.return_value = mock_resp

        engine.session = mock_session
        logged_out = engine.logout()
        self.assertTrue(logged_out)
        self.assertEqual(state_mgr.load().get("status"), pal.Status.DISCONNECTED)

    @patch("portal_autologin.current_ssid", return_value="Other_Network")
    def test_wrong_ssid_skips(self, mock_ssid):
        state_mgr = pal.StateManager(self.state_file)
        engine = pal.AutoLoginEngine(self.config, state_mgr=state_mgr, lock_file=self.lock_file)
        result = engine.step_once()
        self.assertFalse(result)
        self.assertEqual(state_mgr.load().get("status"), pal.Status.UNKNOWN_NETWORK)

    def test_corrupt_state_recovery(self):
        with open(self.state_file, "w") as f:
            f.write("{invalid_json: true")
        state_mgr = pal.StateManager(self.state_file)
        state = state_mgr.load()
        self.assertTrue(state.get("enabled", True))
        self.assertEqual(state.get("status"), pal.Status.INITIALIZING)

    def test_portal_unreachable_in_step_once(self):
        state_mgr = pal.StateManager(self.state_file)
        engine = pal.AutoLoginEngine(self.config, state_mgr=state_mgr, lock_file=self.lock_file)
        with patch("portal_autologin.internet_up", return_value=False), \
             patch("portal_autologin.current_ssid", return_value="MBH2_WiFi"), \
             patch("portal_autologin.portal_available", return_value=False):
            res = engine.step_once()
            self.assertFalse(res)
            self.assertEqual(state_mgr.load().get("status"), pal.Status.DISCONNECTED)

    def test_attempt_login_network_error(self):
        state_mgr = pal.StateManager(self.state_file)
        engine = pal.AutoLoginEngine(self.config, state_mgr=state_mgr, lock_file=self.lock_file)
        mock_session = MagicMock()
        mock_session.get.side_effect = pal.requests.RequestException("DNS lookup failed")
        mock_session.post.side_effect = pal.requests.RequestException("Connection refused")
        engine.session = mock_session
        res = engine.attempt_login()
        self.assertFalse(res)
        self.assertEqual(state_mgr.load().get("status"), pal.Status.DISCONNECTED)

    def test_attempt_login_bad_credentials(self):
        state_mgr = pal.StateManager(self.state_file)
        engine = pal.AutoLoginEngine(self.config, state_mgr=state_mgr, lock_file=self.lock_file)
        mock_session = MagicMock()
        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.text = "<form></form>"
        mock_session.get.return_value = mock_get_resp

        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 200
        mock_post_resp.text = "Invalid username or password"
        mock_session.post.return_value = mock_post_resp
        engine.session = mock_session

        res = engine.attempt_login()
        self.assertFalse(res)
        self.assertEqual(state_mgr.load().get("status"), pal.Status.AUTH_FAILED)

    def test_attempt_login_quota_exceeded(self):
        state_mgr = pal.StateManager(self.state_file)
        engine = pal.AutoLoginEngine(self.config, state_mgr=state_mgr, lock_file=self.lock_file)
        mock_session = MagicMock()
        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.text = "<form></form>"
        mock_session.get.return_value = mock_get_resp

        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 200
        mock_post_resp.text = "Bandwidth limit exceeded"
        mock_session.post.return_value = mock_post_resp
        engine.session = mock_session

        res = engine.attempt_login()
        self.assertFalse(res)
        self.assertEqual(state_mgr.load().get("status"), pal.Status.QUOTA_EXCEEDED)

    def test_load_config_missing_raises(self):
        with self.assertRaises(FileNotFoundError):
            pal.load_config("/nonexistent/path/config.ini")

    def test_load_config_valid(self):
        cfg_path = os.path.join(self.temp_dir.name, "valid.ini")
        with open(cfg_path, "w") as f:
            f.write("[credentials]\nusername=u\npassword=p\n[settings]\nportal_url=http://1.1.1.1\n")
        cfg = pal.load_config(cfg_path)
        self.assertEqual(cfg["credentials"]["username"], "u")

    @patch("sys.argv", ["portal_autologin.py", "--toggle-on"])
    def test_main_toggle_on(self):
        with patch("portal_autologin.StateManager") as MockStateMgr:
            mock_inst = MagicMock()
            MockStateMgr.return_value = mock_inst
            pal.main()
            mock_inst.set_enabled.assert_called_with(True)

    @patch("sys.argv", ["portal_autologin.py", "--toggle-off"])
    def test_main_toggle_off(self):
        with patch("portal_autologin.StateManager") as MockStateMgr:
            mock_inst = MagicMock()
            MockStateMgr.return_value = mock_inst
            pal.main()
            mock_inst.set_enabled.assert_called_with(False)

    @patch("sys.argv", ["portal_autologin.py", "--status-json"])
    def test_main_status_json(self):
        with patch("portal_autologin.StateManager") as MockStateMgr:
            mock_inst = MagicMock()
            mock_inst.load.return_value = {"enabled": True, "status": "CONNECTED"}
            MockStateMgr.return_value = mock_inst
            pal.main()
            mock_inst.load.assert_called()

    def test_setup_logging(self):
        pal.setup_logging(verbose=True)

    def test_logout_enters_standby(self):
        state_mgr = pal.StateManager(self.state_file)
        engine = pal.AutoLoginEngine(self.config, state_mgr=state_mgr, lock_file=self.lock_file)
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_session.get.return_value = mock_resp
        engine.session = mock_session

        self.assertFalse(state_mgr.is_in_standby())
        engine.logout()
        self.assertTrue(state_mgr.is_in_standby())
        self.assertIn("Session released for phone", state_mgr.load().get("status_message", ""))

    @patch("portal_autologin.requests.get")
    def test_internet_up_paths(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 204
        mock_get.return_value = mock_resp
        self.assertTrue(pal.internet_up("http://example.com/204"))

        mock_resp.status_code = 200
        self.assertFalse(pal.internet_up("http://example.com/204"))

        mock_get.side_effect = pal.requests.RequestException("Network unreachable")
        self.assertFalse(pal.internet_up("http://example.com/204"))

    @patch("portal_autologin.requests.get")
    def test_portal_available_paths(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp
        self.assertTrue(pal.portal_available("http://172.20.28.1:8002"))

        mock_resp.status_code = 302
        self.assertTrue(pal.portal_available("http://172.20.28.1:8002"))

        mock_resp.status_code = 500
        self.assertFalse(pal.portal_available("http://172.20.28.1:8002"))

        mock_get.side_effect = pal.requests.RequestException("Host down")
        self.assertFalse(pal.portal_available("http://172.20.28.1:8002"))

    @patch("portal_autologin.subprocess.check_output")
    def test_wifi_interface_detection(self, mock_subp):
        sample_ports = "Hardware Port: Bluetooth PAN\nDevice: en3\n\nHardware Port: Wi-Fi\nDevice: en0\n"
        mock_subp.return_value = sample_ports
        self.assertEqual(pal.wifi_interface(), "en0")

    @patch("portal_autologin.wifi_interface", return_value="en0")
    @patch("portal_autologin.subprocess.check_output")
    def test_current_ssid_detection(self, mock_subp, mock_iface):
        mock_subp.return_value = "Current Wi-Fi Network: Campus_5G\n"
        self.assertEqual(pal.current_ssid(), "Campus_5G")


if __name__ == "__main__":
    unittest.main()
