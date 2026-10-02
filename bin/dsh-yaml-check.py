#!/usr/bin/env python3
"""Validate a DSH profile patch file (`cordis.patch.yml`).

Why this exists: `install.sh` and `uninstall.sh` edit the user's profile patch,
and a patch that does not parse is fatal for the whole web profile — every boot
would fail until the file is repaired by hand. The editors therefore have to
check their own work, and v0.0.4 did that with `yaml.safe_load`, which rejects
DSH's own dialect: DSH 0.2 accepts `!!js` expressions in this file (see
`dsh-app-boot`: "`!!js` expressions allowed"), and PyYAML has no constructor for
that tag. A perfectly valid patch was therefore reported as broken and the
plugin installation rolled back for no reason.

Strategy:
  1. PyYAML present -> parse the file with a SafeLoader extended to accept the
     non-standard tags DSH defines. This is authoritative and catches real
     syntax errors (bad indentation, stray brackets, duplicate structure).
  2. PyYAML absent  -> a conservative structural smoke check. It only rejects
     things that are certainly wrong for a patch file (a tab in the
     indentation, or a root that is not a sequence) and says on stderr that the
     deep check was skipped. A hand-rolled YAML parser would be worse than
     nothing: it rejected valid block scalars in v0.0.4.

Exit codes:
  0  valid (or nothing indicated corruption)
  1  broken
  2  the file could not be read

Usage: dsh-yaml-check.py <file>
"""

from __future__ import annotations

import sys
from pathlib import Path


def check_with_pyyaml(text: str) -> bool | None:
    """Parse with PyYAML when it is installed; None when it is not.

    @param text - the file contents.
    @returns True (valid), False (broken) or None (PyYAML unavailable).
    """
    try:
        import yaml
    except ImportError:
        return None

    class DshPatchLoader(yaml.SafeLoader):
        """SafeLoader that tolerates DSH's extra tags (`!!js`, ...)."""

    def _unknown_tag(loader, suffix, node):  # noqa: ANN001 - PyYAML callback
        # DSH evaluates these expressions itself; for validation they are data.
        return None

    # Standard tags keep their exact constructors; only unknown tags (such as
    # `tag:yaml.org,2002:js`) fall through to these prefixes.
    DshPatchLoader.add_multi_constructor("!", _unknown_tag)
    DshPatchLoader.add_multi_constructor("tag:yaml.org,2002:", _unknown_tag)

    try:
        yaml.load(text, Loader=DshPatchLoader)
        return True
    except Exception as e:  # noqa: BLE001 - any parser error means "broken"
        print(f"yaml: {e}", file=sys.stderr)
        return False


def check_structure(text: str) -> bool:
    """Conservative check for the two failures that matter for a patch file.

    A profile patch is an entry LIST (DSH's own template is `[]`), so:
      - a tab in the indentation is invalid YAML, and
      - a mapping root cannot accept the `- insert:` block this project adds.

    @param text - the file contents.
    @returns True when nothing indicates corruption.
    """
    lines = text.splitlines()
    for number, line in enumerate(lines, 1):
        indent = line[: len(line) - len(line.lstrip())]
        if "\t" in indent:
            print(f"line {number}: tab in indentation", file=sys.stderr)
            return False
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped == "[]":
            continue
        if stripped.startswith("- "):
            return True
        print(f"patch root is not a sequence: {stripped[:60]!r}", file=sys.stderr)
        return False
    return True


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    path = Path(argv[1])
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"cannot read {path}: {e}", file=sys.stderr)
        return 2

    verdict = check_with_pyyaml(text)
    if verdict is not None:
        return 0 if verdict else 1

    print("dsh-yaml-check: PyYAML is not installed; running the structural check only",
          file=sys.stderr)
    return 0 if check_structure(text) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
