"""Shared local configuration, safe diagnostics and atomic artifacts."""
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import json
import os
import sys
import tempfile
import warnings
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent

class WorkflowError(Exception):
    """Messages are static developer-authored descriptions, never remote values."""


def load_environment():
    load_dotenv(ROOT / '.env', override=False, interpolate=False)


def required(name):
    value = os.getenv(name, '').strip()
    if not value:
        raise WorkflowError('Required configuration is missing; check .env.example.')
    return value


def flag(name, default=False):
    value = os.getenv(name, str(default)).strip().lower()
    if value not in ('true', 'false'):
        raise WorkflowError('Boolean configuration must be true or false.')
    return value == 'true'


def integer(name, default, minimum=1, maximum=300000):
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        raise WorkflowError('Numeric configuration is invalid.') from None
    if not minimum <= value <= maximum:
        raise WorkflowError('Numeric configuration is outside its allowed range.')
    return value


def report_date(value=None):
    mode = value or os.getenv('REPORT_DATE_MODE', 'yesterday')
    today = datetime.now(ZoneInfo(os.getenv('REPORT_TIMEZONE', 'Asia/Karachi'))).date()
    if mode == 'yesterday':
        return today - timedelta(days=1)
    if mode == 'today':
        return today
    try:
        return date.fromisoformat(mode)
    except ValueError:
        raise WorkflowError('Report date must be today, yesterday or YYYY-MM-DD.') from None


def base_directory():
    path = Path(os.getenv('DOWNLOAD_BASE_DIR', 'data')).expanduser()
    return path if path.is_absolute() else ROOT / path


def atomic_bytes(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def atomic_json(path, content):
    atomic_bytes(path, json.dumps(content, ensure_ascii=False, allow_nan=False, indent=2).encode('utf-8'))


@contextmanager
def job_lock(base, day):
    lock = Path(base) / 'state' / f'{day.isoformat()}.lock'
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock.mkdir()
    except FileExistsError:
        raise WorkflowError('A job lock exists. Confirm no job is running before removing a stale lock.') from None
    try:
        yield
    finally:
        lock.rmdir()


def diagnostic(name):
    print(f'STAGE: {name}', flush=True)


def run_cli(function):
    # Third-party exceptions can include SQL, SMTP responses, file contents and secrets.
    # Only fixed diagnostic labels are printed; private artifacts stay on local disk.
    try:
        diagnostic('configuration')
        load_environment()
        with warnings.catch_warnings():
            # Library warnings can interpolate workbook values; never emit them.
            warnings.simplefilter('ignore')
            function()
        print('SUCCESS: workflow completed.', flush=True)
        return 0
    except KeyboardInterrupt:
        print('CANCELLED: workflow interrupted.', file=sys.stderr)
        return 130
    except Exception:
        print('FAILED: see the last STAGE label; check local configuration and README. Sensitive details suppressed.', file=sys.stderr)
        return 1
