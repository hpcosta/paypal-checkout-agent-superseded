#!/usr/bin/env python3
"""Small, dependency-free secret scan for the working tree, the staged diff and git history.

Usage:
  python3 scripts/secret_scan.py                 scan the working tree
  python3 scripts/secret_scan.py --history       also scan every added line in all of git history
  python3 scripts/secret_scan.py --staged        scan only the staged diff (used by .githooks/pre-commit)
  python3 scripts/secret_scan.py --gitleaks      also run gitleaks when it is installed

A line may carry the marker `secret-scan: allow` (used only for obviously fake test fixtures).
Findings print file:line and the rule name, never the matched value. Exit status 1 on any finding.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys

ALLOW = "secret-scan: allow"
RULES = [
    ("anthropic-api-key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai-style-key", re.compile(r"\bsk-[A-Za-z0-9]{32,}")),
    ("private-key-block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws-access-key-id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("bearer-token", re.compile(r"Bearer\s+[A-Za-z0-9._\-]{30,}")),
    ("quoted-credential-assignment", re.compile(
        r"""(?i)(secret|token|passw(or)?d|api[_\-]?key|client[_\-]?secret)\w*\s*[:=]\s*["'][A-Za-z0-9/+_\-=.]{16,}["']""")),
]
# PayPal REST app client ids/secrets are ~80 chars, start with A or E, and mix case and digits.
_PAYPAL_LIKE = re.compile(r"\b[AE][A-Za-z0-9_\-]{70,100}\b")


def scan_line(line: str):
    if ALLOW in line:
        return []
    hits = [name for name, rx in RULES if rx.search(line)]
    for m in _PAYPAL_LIKE.finditer(line):
        tok = m.group(0)
        if any(c.isdigit() for c in tok) and any(c.islower() for c in tok) and any(c.isupper() for c in tok):
            hits.append("paypal-credential-shape")
            break
    return hits


def _is_text(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return b"\0" not in fh.read(2048)
    except OSError:
        return False


def scan_tree(root: str):
    findings = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__", ".venv", "venv")]
        for fn in filenames:
            path = os.path.join(dirpath, fn)
            if not _is_text(path):
                continue
            with open(path, encoding="utf-8", errors="replace") as fh:
                for n, line in enumerate(fh, 1):
                    for rule in scan_line(line):
                        findings.append((os.path.relpath(path, root), n, rule))
    return findings


def _scan_diff(root: str, git_args: list):
    out = subprocess.run(["git", "-C", root, *git_args], capture_output=True, text=True, errors="replace", check=True).stdout
    findings, current, commit = [], "?", ""
    for line in out.splitlines():
        if line.startswith("commit "):
            commit = line.split()[1][:10]
        elif line.startswith("+++ b/"):
            current = line[6:]
        elif line.startswith("+") and not line.startswith("+++"):
            for rule in scan_line(line[1:]):
                findings.append((f"{current}@{commit}" if commit else current, 0, rule))
    return findings


def scan_history(root: str):
    return _scan_diff(root, ["log", "--all", "-p", "--no-color", "--no-ext-diff", "--format=commit %H"])


def scan_staged(root: str):
    return _scan_diff(root, ["diff", "--cached", "-U0", "--no-color", "--no-ext-diff"])


def run_gitleaks(root: str):
    exe = shutil.which("gitleaks")
    if not exe:
        print("gitleaks: not installed (regex scan only)")
        return 0
    r = subprocess.run([exe, "detect", "--source", root, "--no-banner", "--redact"], capture_output=True, text=True)
    print(f"gitleaks: exit {r.returncode}")
    return r.returncode


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--history", action="store_true")
    ap.add_argument("--staged", action="store_true")
    ap.add_argument("--gitleaks", action="store_true")
    a = ap.parse_args(argv)
    findings = scan_staged(a.root) if a.staged else scan_tree(a.root)
    scope = "staged diff" if a.staged else "working tree"
    if a.history:
        findings += scan_history(a.root)
        scope += " + git history"
    for where, n, rule in findings:
        print(f"FINDING {where}:{n} {rule}")
    rc = 1 if findings else 0
    if a.gitleaks:
        rc = rc or run_gitleaks(a.root)
    print(f"secret scan ({scope}): {len(findings)} finding(s)")
    return rc


if __name__ == "__main__":
    sys.exit(main())
