"""Parser for the restricted YAML subset the task files use.

The task files are not strict YAML (an unquoted `: ` inside a command), so PyYAML rejects them.
This is the same small parser the benchmark repo uses (scripts/validate_tasks.py), copied so this
project does not depend on that repo.
"""
from __future__ import annotations

import re
from typing import Any


def parse_simple_yaml(text: str) -> dict[str, Any]:
    """Parse a restricted YAML subset used by task fixtures."""
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(0, root)]
    lines = text.splitlines()
    i = 0
    key_re = re.compile(r"^(\s*)([A-Za-z0-9_]+):\s*(.*)$")

    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        m = key_re.match(line)
        if not m:
            continue

        indent, key, rest = len(m.group(1)), m.group(2), m.group(3)
        while stack and indent < stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]

        if rest == "|":
            block: list[str] = []
            while i < len(lines):
                nxt = lines[i]
                if nxt.strip() == "":
                    block.append("")
                    i += 1
                    continue
                nxt_indent = len(nxt) - len(nxt.lstrip(" "))
                if nxt_indent <= indent and nxt.strip():
                    break
                block.append(nxt[nxt_indent:] if nxt_indent > indent else nxt.lstrip())
                i += 1
            value: Any = "\n".join(block).rstrip("\n")
        elif rest == "":
            value = {}
            stack.append((indent + 2, value))
        elif rest.startswith("- "):
            # Start of inline list under key — collect subsequent `- item` lines
            items = [rest[2:].strip()]
            while i < len(lines):
                nxt = lines[i]
                if re.match(r"^\s+-\s+", nxt):
                    items.append(nxt.strip()[2:].strip())
                    i += 1
                else:
                    break
            value = items
        else:
            value = rest.strip()
            # Unwrap one matching quote pair only; the old chained strip()
            # also ate lone leading/trailing quotes, which truncated values
            # like `sh -c "a && b"` into an unbalanced command.
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if value.isdigit():
                value = int(value)

        if isinstance(value, dict) and key in parent and isinstance(parent[key], dict):
            stack.append((indent + 2, parent[key]))
            continue

        parent[key] = value
        if isinstance(value, dict):
            stack.append((indent + 2, value))

    return root
