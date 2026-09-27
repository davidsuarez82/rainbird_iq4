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
# Every other language is checked against strings.json for missing or stray
# keys and for placeholders such as {program}, which a translation must keep
# for the entity name to render at all.
translation_problems=$(COMP="$COMP" python3 - <<'PY'
import json, os, pathlib, re

base = pathlib.Path(os.environ["COMP"])

def flatten(data, prefix=""):
    flat = {}
    for key, value in data.items():
        if isinstance(value, dict):
            flat.update(flatten(value, f"{prefix}{key}."))
        else:
            flat[prefix + key] = value
    return flat

reference = flatten(json.loads((base / "strings.json").read_text()))
problems = []
for path in sorted((base / "translations").glob("*.json")):
    if path.name == "en.json":
        continue
    try:
        translation = flatten(json.loads(path.read_text()))
    except ValueError as err:
        problems.append(f"{path.name}: invalid JSON ({err})")
        continue
    for key in sorted(set(reference) - set(translation)):
        problems.append(f"{path.name}: missing {key}")
    for key in sorted(set(translation) - set(reference)):
        problems.append(f"{path.name}: unknown key {key}")
    for key, value in sorted(translation.items()):
        if key in reference:
            expected = set(re.findall(r"\{(\w+)\}", reference[key]))
            if expected != set(re.findall(r"\{(\w+)\}", value)):
                problems.append(f"{path.name}: {key} should keep the placeholders {sorted(expected)}")
print("\n".join(problems))
PY
)
languages=$(ls "$COMP"/translations/*.json | grep -v '/en.json$' | wc -l)
if [ -n "$translation_problems" ]; then
  fail "$translation_problems"
else
  pass "$languages other language file(s) match strings.json"
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
    # LC_ALL=C on all three: under a UTF-8 locale sort ignores spaces and
    # punctuation in its primary comparison while comm compares byte by byte,
    # so the two disagree on what "sorted" means. comm then warns and can
    # abandon the comparison midway, which here would mean reporting no lost
    # definitions when some were in fact removed — the check failing open.
    # Blank lines are dropped before the compare rather than after, since an
    # empty REMOVED or READDED otherwise feeds one in.
    GONE=$(LC_ALL=C comm -23 \
      <(echo "$REMOVED" | sed '/^$/d' | LC_ALL=C sort -u) \
      <(echo "$READDED" | sed '/^$/d' | LC_ALL=C sort -u))
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

echo "[5] entity translation keys"
missing=$(python3 - <<'PY'
import ast, json, pathlib
base = pathlib.Path("custom_components/rainbird_iq4")
strings = json.loads((base / "strings.json").read_text()).get("entity", {})
platforms = {"sensor.py": "sensor", "binary_sensor.py": "binary_sensor",
             "button.py": "button", "calendar.py": "calendar"}
problems, used = [], set()
for filename, domain in platforms.items():
    for node in ast.walk(ast.parse((base / filename).read_text())):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.Assign) and getattr(stmt.targets[0], "id", "") == "_attr_translation_key":
                key = stmt.value.value
                used.add(f"{domain}.{key}")
                if key not in strings.get(domain, {}):
                    problems.append(f"{domain}.{key} used by {node.name} but missing from strings.json")
for domain, keys in strings.items():
    for key in keys:
        if f"{domain}.{key}" not in used:
            problems.append(f"{domain}.{key} in strings.json but used by no entity")
print("\n".join(problems))
PY
)
if [ -n "$missing" ]; then
  fail "$missing"
else
  pass "every translation key matches an entity"
fi
echo

echo "[6] version bumped"
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
