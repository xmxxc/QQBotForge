"""start.sh helpers run under `set -Eeuo pipefail`; a helper's status must not abort a fresh install."""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).absolute().parents[1]


def run_helper(tmp_path, name, setup=''):
    script = (ROOT / 'start.sh').read_text(encoding='utf-8')
    body = re.search(rf'^{name}\(\) \{{\n.*?^\}}\n', script, re.S | re.M).group(0)
    program = (
        'set -Eeuo pipefail\n'
        f'ROOT_DIR={tmp_path}\nVENV_DIR={tmp_path}/.venv\n'
        'step() { printf "%s\\n" "$*"; }\n'
        f'{body}{setup}{name}\necho continued\n'
    )
    return subprocess.run(['bash', '-c', program], capture_output=True, text=True, timeout=30)


def test_missing_venv_does_not_abort_fresh_install(tmp_path):
    result = run_helper(tmp_path, 'backup_invalid_venv')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith('continued')


def test_invalid_venv_is_moved_aside(tmp_path):
    result = run_helper(tmp_path, 'backup_invalid_venv', f'mkdir {tmp_path}/.venv\n')
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / '.venv').exists()
    assert len(list(tmp_path.glob('.venv.backup-*'))) == 1
