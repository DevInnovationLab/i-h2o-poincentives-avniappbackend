#!/usr/bin/env python3
"""
Main.py
-------
The single entry point for the pipeline (the Python counterpart of `Main.R` in
the DIL R repos). It loads `.env`, puts the import root on `sys.path`, and
dispatches to one of the three command-line programs.

    python3 Main.py tables [args]   ->  21-wrangling/avni_to_tables.main(args)
    python3 Main.py media  [args]   ->  21-wrangling/fetch_media.main(args)
    python3 Main.py box    [args]   ->  21-wrangling/sync_box.main(args)

Everything after the sub-command is passed straight through, so the underlying
programs' own `--help` and options work unchanged:

    python3 Main.py tables --out 1-data/12-avni/121-clean/ --subject-type Pump
    python3 Main.py media  --dir 1-data/12-avni/121-clean/ --dry-run
    python3 Main.py box    --dir 1-data/12-avni/121-clean/ --raw <snapshot>.json

Dependencies: standard library only.
"""

import importlib
import os
import sys

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
PROGRAMS = os.path.join(REPO_ROOT, "2-code", "20-programs")
WRANGLING = os.path.join(REPO_ROOT, "2-code", "21-wrangling")

# The import root (see AGENTS.md > The import contract). This is an entry
# script, so adding to sys.path here is allowed; doing it inside the package
# is not.
for _path in (WRANGLING, PROGRAMS):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import envfile  # noqa: E402  (needs PROGRAMS on the path first)

COMMANDS = {
    "tables": ("avni_to_tables", "pull/transform -> CSV tables + HTML report"),
    "media": ("fetch_media", "download the field photos into <out>/media/"),
    "box": ("sync_box", "publish an output directory to Box Drive"),
}


def usage(stream=sys.stdout):
    stream.write("usage: python3 Main.py {%s} [args...]\n\n"
                 % "|".join(COMMANDS))
    for name, (module, blurb) in COMMANDS.items():
        stream.write("  %-7s %s (%s.py)\n" % (name, blurb, module))
    stream.write("\nArguments after the sub-command are passed through "
                 "unchanged; try `python3 Main.py tables --help`.\n")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        usage(sys.stderr)
        return 2
    command, rest = argv[0], argv[1:]
    if command in ("-h", "--help", "help"):
        usage()
        return 0
    if command not in COMMANDS:
        sys.stderr.write("Main.py: unknown command %r\n\n" % command)
        usage(sys.stderr)
        return 2
    envfile.load()
    return importlib.import_module(COMMANDS[command][0]).main(rest)


if __name__ == "__main__":
    sys.exit(main())
