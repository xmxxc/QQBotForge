"""离线协议与提交边界测试，所有凭据均为测试数据。"""

import base64
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import unittest
from unittest.mock import Mock, patch

import requests

import change_password as app


KEY = base64.b64encode(bytes(range(32))).decode("ascii")
CONFIG = {
    "appId": "356959", "baseUrl": "/api/web", "backend": "user.pandadastudio.com",
    "signSecret": KEY, "encryptoSecret": KEY,
}


def response(code=100, *, headers=None, data=None):
    item = Mock(status_code=200, headers=headers or {})
    item.json.return_value = {"code": code, "data": data}
    return item


class ProtocolTests(unittest.TestCase):
    # 向量由本目录原始 vendors 脚本的 15f8 模块计算，使用固定 IV 和测试密钥。
    def test_encrypt_matches_original_javascript(self):
        self.assertEqual(
            app.encrypt_password("Test旧密码123", KEY, bytes(range(16))),
            "AAECAwQFBgcICQoLDA0ODzeh1917gkt5qkHAbD1e8XXYKZ61GIAmo8LN/Jt8xFBm",
        )

    def test_signature_matches_original_javascript(self):
        headers = app.signed_headers(
            CONFIG, "POST", app.CHANGE_PATH,
            b'{"old_pwd":"fake-old","new_pwd":"fake-new"}',
            date="20261023T060708Z", nonce="AAECAwQFBgcICQoLDA0ODw==",
            traceid="EBESExQVFhcYGRobHB0eHw==",
        )
        self.assertEqual(headers["Authorization"], "YH Fdpv60k6qIFYGMrtd9yPZYqHsmw=")

    def test_signed_bytes_are_sent_without_reserialization_or_redirects(self):
        session = Mock()
        session.request.return_value = response()
        payload = {"old_pwd": "a+/=", "new_pwd": "b+/="}
        with patch.object(app, "signed_headers", return_value={}) as sign:
            app.request_api(session, CONFIG, "POST", app.CHANGE_PATH, payload)
        sent = session.request.call_args.kwargs
        self.assertEqual(sent["data"], sign.call_args.args[3])
        self.assertEqual(json.loads(sent["data"]), payload)
        self.assertFalse(sent["allow_redirects"])

    def test_timeout_never_retries_and_reports_unknown_change_result(self):
        session = Mock()
        session.request.side_effect = requests.Timeout("sensitive request details")
        with self.assertRaisesRegex(app.ScriptError, "结果未知"):
            app.request_api(session, CONFIG, "POST", app.CHANGE_PATH, {})
        self.assertEqual(session.request.call_count, 1)

    def test_raw_server_error_is_not_exposed(self):
        session = Mock()
        item = response(1300)
        item.json.return_value["msg"] = "sensitive response token"
        session.request.return_value = item
        with self.assertRaises(app.ScriptError) as raised:
            app.request_api(session, CONFIG, "POST", "/login/pwd", {})
        self.assertNotIn("sensitive", str(raised.exception))
        self.assertIn("1300", str(raised.exception))


class FlowTests(unittest.TestCase):
    def run_main(self, arguments, replies):
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        session.request.side_effect = replies
        output = io.StringIO()
        with (
            patch.object(app.sys, "argv", ["change_password.py", *arguments]),
            patch.object(app.requests, "Session", return_value=session),
            patch.object(app, "load_config", return_value=CONFIG),
            patch("builtins.input", return_value="16600000000"),
            patch.object(app, "hidden_input", side_effect=["old-test", "new-test", "new-test"]),
            redirect_stdout(output), redirect_stderr(output),
        ):
            result = app.main()
        self.assertNotIn("old-test", output.getvalue())
        self.assertNotIn("new-test", output.getvalue())
        return result, session, output.getvalue()

    def test_check_logs_in_but_does_not_change_password(self):
        result, session, _ = self.run_main(["--check"], [response(), response()])
        self.assertEqual(result, 0)
        paths = [call.args[1] for call in session.request.call_args_list]
        self.assertEqual(paths, [app.ORIGIN + "/api/web/login/pwd", app.ORIGIN + "/api/web/user_center/userinfo"])

    def test_success_sends_exactly_one_change_after_login_and_session_check(self):
        result, session, _ = self.run_main([], [response(), response(), response()])
        self.assertEqual(result, 0)
        self.assertEqual(session.request.call_count, 3)
        self.assertTrue(session.request.call_args.args[1].endswith(app.CHANGE_PATH))
        login = json.loads(session.request.call_args_list[0].kwargs["data"])
        self.assertEqual(login["area"], "86")
        self.assertNotEqual(login["pwd"], "old-test")

    def test_login_failure_prevents_password_change(self):
        result, session, _ = self.run_main([], [response(1300)])
        self.assertEqual(result, 1)
        self.assertEqual(session.request.call_count, 1)

    def test_captcha_stops_before_password_change(self):
        result, session, output = self.run_main(
            [], [response(headers={"x-yh-captcha-trigger": "1"})]
        )
        self.assertEqual(result, 1)
        self.assertEqual(session.request.call_count, 1)
        self.assertIn("人机验证", output)

    def test_invalid_new_password_never_reaches_submission(self):
        for password in ["tiny", "with space", "😀" * 9, "x" * 17]:
            with self.subTest(password=password), patch.object(app, "hidden_input", return_value=password):
                with self.assertRaises(app.ScriptError):
                    app.prompt_new_password("old-test")


if __name__ == "__main__":
    unittest.main()
