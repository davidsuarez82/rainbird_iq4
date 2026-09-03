#!/usr/bin/env bash
# Pre-release checks. Run from the repo root after syncing from Home
# Assistant and before committing/tagging.
#
# Exists because of the v1.3.1 regression: start_program and
# set_optimistic_running were silently dropped when a stale HA tree was
# copied over the repo. Git treats a reversion as an ordinary change, so
# nothing flagged it. These checks do.

set -uo pipefail

COMP="custom_components/rainbird_iq4"
FAILED=0

fail() { echo "  FAIL: $*"; FAILED=1; }
pass() { echo "  ok: $*"; }

LAST_TAG=$(git describe --tags --abbrev=0 2>/dev/null)
echo "Comparing against ${LAST_TAG:-<no tag>}"
echo

echo "[1] service surface"
if python3 tools/check_services.py "$COMP"; then :; else FAILED=1; fi
echo

echo "[2] translations in sync"
if diff -q <(jq -S . "$COMP/strings.json") \
          <(jq -S . "$COMP/translations/en.json") >/dev/null; then
  pass "strings.json == translations/en.json"
else
  fail "strings.json and translations/en.json diverge"
fi
echo

echo "[3] no credentials staged"
if git status --porcelain | grep -qi token || git ls-files | grep -qi token; then
  fail "a path matching 'token' is tracked or modified"
else
  pass "no token paths"
fi
echo

# Compare against the last two tags, not just the most recent one. A
# definition dropped in release N-1 and still absent in N shows up as
# unchanged when N-1 is the only baseline, so a regression can survive two
# releases unnoticed -- which is how 1.3.1 shipped.
RECENT_TAGS=$(git tag --sort=v:refname | tail -2 | tr '\n' ' ')
echo "[4] definitions removed since ${RECENT_TAGS:-<no tags>}"
if [ -n "$RECENT_TAGS" ]; then
  ALL_GONE=""
  for TAG in $RECENT_TAGS; do
    REMOVED=$(git diff "$TAG" -- "$COMP" \
      | grep -E '^-\s*(async def|def|class) ' \
      | sed -E 's/^-\s*//' || true)
    READDED=$(git diff "$TAG" -- "$COMP" \
      | grep -E '^\+\s*(async def|def|class) ' \
      | sed -E 's/^\+\s*//' || true)
    GONE=$(comm -23 <(echo "$REMOVED" | sort -u) <(echo "$READDED" | sort -u) | sed '/^$/d')
    if [ -n "$GONE" ]; then
      ALL_GONE=$(printf '%s\n%s' "$ALL_GONE" \
        "$(echo "$GONE" | sed "s|\$|\t$TAG|")")
    fi
  done

  ALL_GONE=$(echo "$ALL_GONE" | sed '/^$/d' \
    | awk -F'\t' '!seen[$1]++ { print $1 "  (missing since " $2 ")" }')
  if [ -n "$ALL_GONE" ]; then
    echo "  REVIEW: these definitions no longer exist. Deliberate?"
    echo "$ALL_GONE" | sed 's/^/    /'
    echo "  (re-run with CONFIRM_REMOVALS=1 once verified)"
    [ "${CONFIRM_REMOVALS:-0}" = "1" ] || FAILED=1
  else
    pass "no definitions lost"
  fi
else
  echo "  skipped: no tag to compare against"
fi
echo

echo "[5] version bumped"
VERSION=$(jq -r .version "$COMP/manifest.json")
if [ -n "$LAST_TAG" ] && git diff --quiet "$LAST_TAG" -- "$COMP"; then
  # Nothing under custom_components/ has changed since the last tag, so this
  # is a tooling or docs commit with nothing to release. Requiring a bump
  # here would force a pointless version just to satisfy the check.
  TOOLING_ONLY=1
  pass "no integration changes since $LAST_TAG, bump not needed"
elif [ "v$VERSION" = "$LAST_TAG" ]; then
  fail "manifest still at $VERSION, same as $LAST_TAG"
elif [ "$(printf '%s\n%s\n' "${LAST_TAG#v}" "$VERSION" | sort -V | tail -1)" != "$VERSION" ]; then
  fail "manifest version $VERSION is older than $LAST_TAG"
else
  pass "manifest at $VERSION"
fi
echo

if [ "$FAILED" -ne 0 ]; then
  echo "BLOCKED - resolve the above before committing."
  exit 1
fi
if [ "${TOOLING_ONLY:-0}" = "1" ]; then
  echo "All checks passed. Safe to commit (no tag or release needed)."
else
  echo "All checks passed. Safe to commit and tag v$VERSION."
fi
