#!/usr/bin/env python3
"""Verify that services.yaml, SERVICES and _SERVICE_HANDLERS agree.

A service described in services.yaml but never registered in
hass.services is invisible to Home Assistant: the UI editor cannot find
it and existing automations fail at runtime. This is exactly the
regression shipped in v1.3.1 (start_program). Run before every release.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

BASE = Path(sys.argv[1] if len(sys.argv) > 1 else "custom_components/rainbird_iq4")


def yaml_services(path: Path) -> set[str]:
    return {
        m.group(1)
        for m in re.finditer(r"^([a-z_][a-z0-9_]*):", path.read_text(), re.M)
    }


def init_services(path: Path) -> tuple[set[str], set[str]]:
    tree = ast.parse(path.read_text())
    declared: set[str] = set()
    handlers: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            name = getattr(target, "id", None)
            if name == "SERVICES" and isinstance(node.value, (ast.List, ast.Tuple)):
                declared = {
                    e.value for e in node.value.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                }
            elif name == "_SERVICE_HANDLERS" and isinstance(node.value, ast.Dict):
                handlers = {
                    k.value for k in node.value.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                }
    return declared, handlers


def main() -> int:
    described = yaml_services(BASE / "services.yaml")
    declared, handlers = init_services(BASE / "__init__.py")

    problems: list[str] = []
    if missing := described - handlers:
        problems.append(
            f"described in services.yaml but NOT registered: {sorted(missing)}"
        )
    if orphan := handlers - described:
        problems.append(
            f"registered but NOT described in services.yaml: {sorted(orphan)}"
        )
    if leak := handlers - declared:
        problems.append(
            f"registered but missing from SERVICES (never unloaded): {sorted(leak)}"
        )
    if stale := declared - handlers:
        problems.append(
            f"listed in SERVICES but has no handler: {sorted(stale)}"
        )

    if problems:
        print("FAIL - service surface mismatch")
        for p in problems:
            print(f"  - {p}")
        return 1

    print(f"OK - {len(handlers)} services consistent across all three sites")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
