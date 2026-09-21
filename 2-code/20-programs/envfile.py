#!/usr/bin/env python3
"""
envfile.py
----------
Load `KEY=VALUE` pairs from the repo-root `.env` into `os.environ`.

This is the Python stand-in for the `.Rprofile` that auto-loads `.env` in the
DIL R repos. Entry points call `load()` once at startup, so every module sees
the same configuration and nobody has to `export` anything by hand.

A real environment variable ALWAYS wins over the file: `load()` uses
`os.environ.setdefault`, so

    AVNI_AUTH_TOKEN="<fresh token>" python3 Main.py tables

overrides whatever `.env` says, which is what you want for a one-off token.

Blank lines and `#` comments are skipped, an optional leading `export` is
stripped, and one layer of surrounding quotes is removed. A missing `.env` is
not an error: the pipeline can be configured entirely from the environment.

USAGE
-----
    import envfile

    envfile.load()                      # <repo root>/.env, auto-detected
    envfile.load("/path/to/other.env")  # anywhere else

Dependencies: standard library only.
"""

import os

# envfile.py lives at <repo root>/2-code/20-programs/envfile.py
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load(path=None):
    """Read `path` (default `<repo root>/.env`) into os.environ; return the keys set."""
    path = path or os.path.join(REPO_ROOT, ".env")
    loaded = []
    if not os.path.exists(path):
        return loaded
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ.setdefault(key, value)
                loaded.append(key)
    return loaded
