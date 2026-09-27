#!/usr/bin/env bash
# Launch DSH Web (the CLI/browser path).
#
# Why this script exists: `dsh` is a Node CLI whose `lib/bin.js` carries the
# shebang `#!/usr/bin/env node`. A desktop session's PATH may not contain
# `node`; and permanently mutating PATH just to locate it leaks a foreign
# runtime into EVERY child process DSH spawns (including the agent's shell
# tool).
#
# This script resolves node once and uses it for this command only; it does
# not modify PATH.
#
# Configurable:
#   DSH_NODE   absolute path to the node interpreter
#   DSH_BIN    path to the dsh entry script (lib/bin.js)

set -euo pipefail

# 1) Locate node: explicit setting first, then PATH, then common locations.
# Standard Node.js with V8 engine is strictly required (rejects Bun masquerading as node).
find_node() {
  local cand
  for cand in \
    "${DSH_NODE:-}" \
    "$(command -v node 2>/dev/null || true)" \
    /usr/bin/node \
    /usr/local/bin/node \
    "$HOME/.local/bin/node" \
    /opt/homebrew/bin/node
  do
    if [ -n "$cand" ] && [ -x "$cand" ]; then
      if "$cand" -e 'process.exit(process.versions.v8 ? 0 : 1)' >/dev/null 2>&1; then
        printf '%s' "$cand"
        return 0
      fi
    fi
  done
  return 1
}

# 2) Locate the dsh entry script: explicit setting, then bun/npm trees, then PATH.
find_dsh() {
  if [ -n "${DSH_BIN:-}" ] && [ -f "${DSH_BIN}" ]; then
    printf '%s' "$DSH_BIN"; return 0
  fi
  local cand
  for cand in \
    "${BUN_INSTALL:-$HOME/.bun}/install/global/node_modules/@deepseek-ai/dsh/lib/bin.js" \
    "$HOME/.local/share/pnpm/global/5/node_modules/@deepseek-ai/dsh/lib/bin.js" \
    /usr/lib/node_modules/@deepseek-ai/dsh/lib/bin.js \
    /usr/local/lib/node_modules/@deepseek-ai/dsh/lib/bin.js
  do
    [ -f "$cand" ] && { printf '%s' "$cand"; return 0; }
  done
  if command -v dsh >/dev/null 2>&1; then
    printf '%s' "$(command -v dsh)"; return 0
  fi
  return 1
}

NODE="$(find_node)" || {
  echo "dsh: Node.js with V8 engine not found. Set DSH_NODE to its absolute path." >&2
  exit 1
}

DSH_ENTRY="$(find_dsh)" || {
  echo "dsh: no @deepseek-ai/dsh installation found." >&2
  echo "     Install it: bun add -g @deepseek-ai/dsh" >&2
  echo "     Or set DSH_BIN to the lib/bin.js path." >&2
  exit 1
}

exec "$NODE" "$DSH_ENTRY" web "$@"
