"""A small, dependency-free S-expression parser for KiCad text formats.

KiCad's .kicad_sch and .kicad_pcb files are S-expressions: nested lists of
atoms and quoted strings, e.g.

    (symbol (lib_id "Device:LED") (property "Reference" "D1" ...))

parse() returns the list of top-level forms. Each form is a Python list whose
first element is the head atom (a str); atoms and quoted strings are both
returned as plain str — Preflight only ever compares them as text, so the
distinction does not matter here. The parser is iterative, so deeply nested
or large board files cannot hit Python's recursion limit.
"""


class SexprError(ValueError):
    """Raised when a file is not a well-formed S-expression."""


_OPEN = object()
_CLOSE = object()


def _tokenize(text):
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r\n":
            i += 1
        elif ch == "(":
            yield _OPEN
            i += 1
        elif ch == ")":
            yield _CLOSE
            i += 1
        elif ch == '"':
            j = i + 1
            buf = []
            while j < n:
                cj = text[j]
                if cj == "\\" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                elif cj == '"':
                    break
                else:
                    buf.append(cj)
                    j += 1
            if j >= n:
                raise SexprError("unterminated string literal")
            yield "".join(buf)
            i = j + 1
        else:
            j = i
            while j < n and text[j] not in ' \t\r\n()"':
                j += 1
            yield text[i:j]
            i = j


def parse(text):
    """Parse S-expression text into a list of top-level forms."""
    root = []
    stack = [root]
    for tok in _tokenize(text):
        if tok is _OPEN:
            node = []
            stack[-1].append(node)
            stack.append(node)
        elif tok is _CLOSE:
            if len(stack) == 1:
                raise SexprError("unbalanced ')' in file")
            stack.pop()
        else:
            stack[-1].append(tok)
    if len(stack) != 1:
        raise SexprError("unbalanced '(' in file (truncated?)")
    return root


def head(node):
    """Head atom of a form, or None if the node is not a non-empty list."""
    if isinstance(node, list) and node and isinstance(node[0], str):
        return node[0]
    return None


def children(node, name):
    """Direct child forms of `node` whose head is `name`."""
    if not isinstance(node, list):
        return
    for child in node[1:]:
        if isinstance(child, list) and head(child) == name:
            yield child


def child(node, name):
    """First direct child form with the given head, or None."""
    for c in children(node, name):
        return c
    return None
