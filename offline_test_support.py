"""Test-only budgets. Never imported by the production automation."""
# Account for shared/low-spec Windows VPS scheduling and Chromium startup work.
# These are upper bounds on condition-based waits, not unconditional sleeps.
import os

try:
    ACTION_TIMEOUT_MS = int(os.getenv('CISEPOS_OFFLINE_TIMEOUT_MS', '15000'))
except ValueError:
    raise ValueError('CISEPOS_OFFLINE_TIMEOUT_MS must be an integer.') from None
if not 5000 <= ACTION_TIMEOUT_MS <= 120000:
    raise ValueError('CISEPOS_OFFLINE_TIMEOUT_MS must be between 5000 and 120000.')
EXPECTED_FAILURE_TIMEOUT_MS = 500
