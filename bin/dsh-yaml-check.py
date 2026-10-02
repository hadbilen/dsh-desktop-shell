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
        data = yaml.load(text, Loader=DshPatchLoader)
    except Exception as e:  # noqa: BLE001 - any parser error means "broken"
        print(f"yaml: {e}", file=sys.stderr)
        return False
    # A patch is an entry LIST (DSH's own template is `[]`). A mapping root parses
    # fine but cannot accept the `- insert:` block, so DSH would fail on it.
    if data is not None and not isinstance(data, list):
        print(f"patch root is {type(data).__name__}, expected a list of entries",
              file=sys.stderr)
        return False
    return True


def strip_comment(line: str) -> str:
    """Drop a trailing YAML comment, ignoring `#` inside quotes.

    Without this, a perfectly valid line such as `sound: true   # don't play`
    was reported as "unterminated quote" (the apostrophe inside the comment
    opened a string), and the caller rolled back a good installation.

    @param line - one source line.
    @returns the line up to (not including) an unquoted comment.
    """
    quote = ""
    index = 0
    while index < len(line):
        char = line[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "#" and (index == 0 or line[index - 1] in " \t"):
            return line[:index]
        index += 1
    return line


def check_structure(text: str) -> bool:
    """Conservative check for the failures that matter for a patch file.

    A profile patch is an entry LIST (DSH's own template is `[]`), so:
      - a tab in the indentation is invalid YAML,
      - a mapping root cannot accept the `- insert:` block this project adds,
      - and unbalanced brackets/quotes are the usual shape of a truncated edit.

    This runs only when PyYAML is absent; it is a smoke check, not a parser.

    @param text - the file contents.
    @returns True when nothing indicates corruption.
    """
    lines = text.splitlines()
    for number, line in enumerate(lines, 1):
        indent = line[: len(line) - len(line.lstrip())]
        if "\t" in indent:
            print(f"line {number}: tab in indentation", file=sys.stderr)
            return False

    # The root must be a sequence: the first content line is a `- ` item.
    # `---`/`...` document markers and comments are not content.
    for number, line in enumerate(lines, 1):
        stripped = strip_comment(line).strip()
        if not stripped or stripped in ("---", "...") or stripped == "[]":
            continue
        if not stripped.startswith("- ") and stripped != "-":
            print(f"line {number}: patch root is not a sequence: {stripped[:60]!r}",
                  file=sys.stderr)
            return False
        break

    # Unbalanced brackets/quotes are the other shape a broken patch takes. Quotes
    # are tracked so `[` inside a string (a JS expression in a `!!js` tag, say)
    # does not count.
    depth = 0
    quote = ""
    for number, line in enumerate(lines, 1):
        code = strip_comment(line)
        index = 0
        while index < len(code):
            char = code[index]
            if quote:
                if char == "\\":
                    index += 2
                    continue
                if char == quote:
                    quote = ""
            elif char in "\"'":
                quote = char
            elif char in "[{":
                depth += 1
            elif char in "]}":
                depth -= 1
                if depth < 0:
                    print(f"line {number}: unbalanced closing bracket", file=sys.stderr)
                    return False
            index += 1
    if depth != 0 or quote:
        print("unbalanced brackets or an unterminated quote", file=sys.stderr)
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
