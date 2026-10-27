"""Run the ten untouched upstream unittest cases under the socket guard."""

import hashlib
import importlib.util
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[2] / 'docs/vendor/issue70'
HASHES = {
    'change_password.py': '189ba280ce87f69ae170947553a4aede3bbf42b74039989c15986ad9540df701',
    'test_change_password.py': '219e504bccac516a9d2b4db2c20191459c0a737dc011fd2ddd3a5c6b01ff0a57',
}


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_source_hashes():
    for name, digest in HASHES.items():
        assert hashlib.sha256((SOURCE / name).read_bytes()).hexdigest() == digest


CASES = [
    ('ProtocolTests', 'test_encrypt_matches_original_javascript'),
    ('ProtocolTests', 'test_signature_matches_original_javascript'),
    ('ProtocolTests', 'test_signed_bytes_are_sent_without_reserialization_or_redirects'),
    ('ProtocolTests', 'test_timeout_never_retries_and_reports_unknown_change_result'),
    ('ProtocolTests', 'test_raw_server_error_is_not_exposed'),
    ('FlowTests', 'test_check_logs_in_but_does_not_change_password'),
    ('FlowTests', 'test_success_sends_exactly_one_change_after_login_and_session_check'),
    ('FlowTests', 'test_login_failure_prevents_password_change'),
    ('FlowTests', 'test_captcha_stops_before_password_change'),
    ('FlowTests', 'test_invalid_new_password_never_reaches_submission'),
]


@pytest.mark.parametrize(('group', 'method'), CASES, ids=[method for _, method in CASES])
def test_original_case(original, group, method):
    getattr(original, group)(method).debug()
