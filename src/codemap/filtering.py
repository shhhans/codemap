"""Project-dependent relation filtering & third-party stubbing (Milestone 1, V2).

Two defensive nets keep the LLM's context from drowning in low-level library
plumbing *before* a Worker ever asks the model about a downstream node:

  1. **Global relation filter** — a hardcoded blacklist of builtin / stdlib call
     names (``len``, ``print``, ``dict.get`` …) that are never mainline stations.
     Pruned outright, no LLM round-trip, no graph descent.

  2. **Local stubbing** — parse a file's ``import`` statements to learn which
     top-level modules are *third-party* (and which are stdlib). When tracing
     touches a symbol that resolves into one of those modules we treat it as a
     black-box **Stub**: we never expand its AST. Instead we classify the
     boundary purely by its edges —

         no out-edges  → **Sink**  (material is absorbed; stop)
         has out-edges → **Dual**  (a pass-through transformer; keep tracing)

Design note on the graph engine: Codebase-Memory (Tree-Sitter) indexes the
repository's *own* source, so external library symbols are usually absent from
the graph entirely — meaning the engine already refuses to "expand their AST"
for us. This module is therefore written to be correct in *both* worlds: if a
library node never surfaces as a candidate, stubbing is a harmless no-op; if the
engine *does* surface it (qualified like ``numpy.array`` or aliased like
``np.array``), :meth:`StubModules.is_stub_call` catches it and
:func:`classify_boundary` decides Sink vs Dual from edge count alone.

Pure stdlib (``ast`` + ``sys``); no MCP/LLM dependency, so the whole module is
unit-testable offline.
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass, field

# ── 1. Global relation filter ───────────────────────────────────────────────
# Builtins, common dunder/dict/list/str methods, and control-flow keywords that
# show up as `name(` in source but are never a business-mainline station. Lifted
# from worker._CALL_NOISE and extended so the Worker and the recovery path share
# one source of truth.
GLOBAL_RELATION_BLACKLIST: frozenset[str] = frozenset({
    # control-flow keywords that the source-regex recovery can mistake for calls
    "if", "for", "while", "return", "and", "or", "not", "in", "is", "lambda",
    # builtin constructors / coercions
    "int", "str", "float", "bool", "dict", "list", "set", "tuple", "bytes",
    "frozenset", "complex", "type", "object",
    # builtin functions
    "print", "len", "range", "super", "isinstance", "issubclass", "callable",
    "getattr", "setattr", "hasattr", "delattr", "enumerate", "zip", "map",
    "filter", "max", "min", "sum", "abs", "round", "sorted", "reversed", "any",
    "all", "open", "format", "repr", "hash", "id", "iter", "next", "vars",
    "dir", "input", "ord", "chr", "hex", "oct", "bin", "divmod", "pow",
    # ubiquitous container / string methods (short-name collisions)
    "join", "split", "strip", "lstrip", "rstrip", "items", "keys", "values",
    "append", "extend", "insert", "pop", "get", "add", "update", "remove",
    "put_nowait", "get_nowait", "startswith", "endswith", "replace", "find",
    "index", "count", "lower", "upper", "encode", "decode", "rsplit",
})


def is_global_noise(name: str) -> bool:
    """True if a (short) call name is hardcoded plumbing to be pruned outright."""
    return name in GLOBAL_RELATION_BLACKLIST


# Python stdlib top-level module names. We treat stdlib imports as stubs too (we
# never expand into them), but we still distinguish them from genuinely
# third-party modules so callers can report "third-party" precisely.
def _stdlib_roots() -> frozenset[str]:
    names = set(getattr(sys, "stdlib_module_names", set()))  # 3.10+
    # A small floor for older interpreters / safety; the set above is canonical.
    names.update({
        "os", "sys", "re", "json", "math", "time", "typing", "dataclasses",
        "collections", "itertools", "functools", "pathlib", "asyncio", "logging",
        "sqlite3", "abc", "contextlib", "enum", "io", "subprocess", "shlex",
    })
    return frozenset(n for n in names if not n.startswith("_"))


PYTHON_STDLIB_ROOTS: frozenset[str] = _stdlib_roots()


# ── 2. Local stubbing (import-aware) ────────────────────────────────────────
@dataclass
class StubModules:
    """Imported top-level modules of one file, with their local aliases.

    Built by :func:`parse_stub_modules`. ``aliases`` maps every *local* name a
    call site might use (the module itself, an ``as`` alias, or a
    ``from x import y`` binding) back to its canonical top-level root, so we can
    resolve ``np.array`` / ``numpy.array`` / a bare ``array`` (from
    ``from numpy import array``) all to root ``numpy``.
    """

    roots: set[str] = field(default_factory=set)            # {"numpy", "os", ...}
    aliases: dict[str, str] = field(default_factory=dict)   # local name -> root
    from_bindings: dict[str, str] = field(default_factory=dict)  # bound name -> root

    @property
    def third_party(self) -> set[str]:
        """Roots that are not part of the Python standard library."""
        return {r for r in self.roots if r not in PYTHON_STDLIB_ROOTS}

    def root_of(self, ref: str) -> str | None:
        """Resolve a called name / qualified_name to an imported module root.

        Handles three shapes:
          • ``numpy.array`` / ``np.array`` — dotted, first segment is a module
            alias.
          • a bare name bound by ``from numpy import array`` → ``array``.
        Returns the canonical root (e.g. ``numpy``) or ``None`` if the symbol
        does not belong to any imported module.
        """
        if not ref:
            return None
        head = ref.split(".", 1)[0]
        if head in self.aliases:
            return self.aliases[head]
        # A bare imported binding (from-import) with no module prefix.
        if "." not in ref and ref in self.from_bindings:
            return self.from_bindings[ref]
        return None

    def is_stub_call(self, ref: str) -> bool:
        """True if ``ref`` resolves into any imported module (stub boundary)."""
        return self.root_of(ref) is not None

    def is_third_party_call(self, ref: str) -> bool:
        """True if ``ref`` resolves specifically into a third-party module."""
        root = self.root_of(ref)
        return root is not None and root not in PYTHON_STDLIB_ROOTS

    def externals_only(self, first_party_roots: set[str]) -> "StubModules":
        """A copy with first-party imports removed.

        The project's *own* top-level packages (e.g. ``codemap``) are imported
        like any other module but resolve to internal, traceable nodes — they
        must not be stubbed. Callers pass the set of package roots that exist in
        the indexed graph so only genuine external (stdlib/third-party) modules
        remain.
        """
        keep = self.roots - first_party_roots
        return StubModules(
            roots=set(keep),
            aliases={a: r for a, r in self.aliases.items() if r in keep},
            from_bindings={n: r for n, r in self.from_bindings.items() if r in keep},
        )


def parse_stub_modules(source: str) -> StubModules:
    """Parse a Python source file's imports into a :class:`StubModules`.

    Resilient to syntax errors (returns whatever imports parsed up to the error
    via a best-effort line fallback) so a half-written file never kills a trace.
    """
    stub = StubModules()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return _parse_imports_line_fallback(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                stub.roots.add(root)
                # `import numpy as np` → np; `import numpy` → numpy. Always also
                # map root→root: code uses the alias, but the *graph* may surface
                # the canonical qualified name (e.g. `numpy.linalg.norm`).
                stub.aliases[alias.asname or root] = root
                stub.aliases.setdefault(root, root)
        elif isinstance(node, ast.ImportFrom):
            if node.level and not node.module:
                continue  # bare relative `from . import x` — same package, skip
            module = node.module or ""
            root = module.split(".", 1)[0]
            if node.level:  # relative import: belongs to this package, not a stub
                continue
            if not root:
                continue
            stub.roots.add(root)
            stub.aliases[root] = root
            for alias in node.names:
                if alias.name == "*":
                    continue
                stub.from_bindings[alias.asname or alias.name] = root
    return stub


def _parse_imports_line_fallback(source: str) -> StubModules:
    """Last-resort import scan when the file won't parse as a whole."""
    stub = StubModules()
    for raw in source.splitlines():
        line = raw.strip()
        try:
            node = ast.parse(line).body[0]  # type: ignore[index]
        except (SyntaxError, IndexError):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            merged = parse_stub_modules(line)
            stub.roots |= merged.roots
            stub.aliases.update(merged.aliases)
            stub.from_bindings.update(merged.from_bindings)
    return stub


_DOTTED_CALL = re.compile(r"\b([A-Za-z_]\w*)\s*\.\s*([A-Za-z_]\w*)\s*\(")


def stub_call_names(source: str, stubs: StubModules) -> set[str]:
    """Short call names in ``source`` that belong to an imported stub module.

    Empirically (see the M1 probe) Codebase-Memory indexes only repo-internal
    symbols, so a third-party call like ``np.dot(x)`` never becomes a graph node
    — but its *bare* attribute name (``dot``) is still pulled out by the
    source-regex recovery path, where it can collide with a unique internal
    ``dot()`` and produce a phantom edge. This returns those attribute names
    (plus bare ``from``-import bindings) so recovery can skip them.

    Example: with ``import numpy as np``, ``np.dot(x)`` → ``{"dot"}``.
    """
    names: set[str] = set()
    for head, attr in _DOTTED_CALL.findall(source):
        if stubs.root_of(head) is not None:  # head is an imported module/alias
            names.add(attr)
    # Bare names bound by `from numpy import array` are stub calls with no prefix.
    names |= set(stubs.from_bindings)
    return names


# ── 3. Boundary classification (Sink vs Dual) ───────────────────────────────
SINK = "sink"
DUAL = "dual"


def classify_boundary(out_edge_count: int) -> str:
    """Classify a stubbed boundary call from its out-edge count alone.

    A Stub is a black box we refuse to expand, so the only signal is whether
    material continues to flow out of it:

      • ``out_edge_count == 0`` → :data:`SINK`  — absorbs the material (a 防爆墙
        terminal): persistence, logging, a pure sink. Stop tracing.
      • ``out_edge_count > 0``  → :data:`DUAL`  — a transparent transformer that
        forwards material onward (数据透传转换器): keep tracing its out-edges.

    Defensive default: when the graph has no node for the stub at all (the common
    case, since external symbols are usually unindexed), the caller passes 0 and
    we treat it as a Sink — the safe, context-bounded choice.
    """
    return DUAL if out_edge_count > 0 else SINK
