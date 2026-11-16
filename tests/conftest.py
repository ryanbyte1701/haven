import os

# The API refuses settings/automation writes when no dashboard password is set; most tests run
# without one, so allow it here (test_security_v2 checks the refusal itself).
os.environ.setdefault("HAVEN_ALLOW_NO_PASSWORD", "1")
