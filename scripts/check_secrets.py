"""Pre-commit / CI guard: refuse to commit anything that looks like a credential.

    python scripts/check_secrets.py [files...]     # no args = every tracked file

Deliberately simple (regexes, no dependencies) so it is easy to explain. It
catches the secrets this project actually uses: Anthropic keys, PayPal client
secrets/ids pasted into code, bearer tokens, private keys, and a committed .env.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PATTERNS = {
    "Anthropic API key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "PayPal client id/secret": re.compile(
        r"(?i)paypal_client_(id|secret)\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{40,}"
    ),
    "Bearer token": re.compile(r"Bearer\s+A21AA[A-Za-z0-9_\-]{20,}"),
    "Private key": re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "Generic secret assignment": re.compile(
        r"(?i)(api[_-]?key|secret|token)\s*=\s*['\"][A-Za-z0-9_\-]{32,}['\"]"
    ),
}
ALLOWED_FILES = {"scripts/check_secrets.py"}
FORBIDDEN_NAMES = {".env"}


def tracked_files() -> list[str]:
    cmd = ["git", "ls-files"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)  # noqa: S603, S607
    return out.stdout.split()


def scan(paths: list[str]) -> list[str]:
    problems = []
    for p in paths:
        path = Path(p)
        if path.name in FORBIDDEN_NAMES:
            problems.append(f"{p}: .env files must never be committed")
            continue
        if p in ALLOWED_FILES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary (model artifacts, images)
        for label, rx in PATTERNS.items():
            for m in rx.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                problems.append(f"{p}:{line}: looks like a {label}")
    return problems


def main(argv: list[str]) -> int:
    problems = scan(argv or tracked_files())
    for msg in problems:
        print(msg, file=sys.stderr)
    if problems:
        print("Secret check failed. Move the value to .env / Render env vars.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
