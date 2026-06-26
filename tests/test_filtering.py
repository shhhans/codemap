"""M1 tests — global relation filtering, import-aware stubbing, Sink/Dual.

Pure stdlib; no MCP/LLM. Exercises the two defensive nets and the boundary
classifier in isolation, plus the alias/from-import resolution edge cases that
let stubbing work whether or not the graph surfaces external symbols.
"""

from __future__ import annotations

from codemap.filtering import (
    DUAL,
    GLOBAL_RELATION_BLACKLIST,
    SINK,
    classify_boundary,
    is_global_noise,
    parse_stub_modules,
    stub_call_names,
)


# ── Global relation filter ──────────────────────────────────────────────────
def test_global_noise_catches_builtins_and_container_methods() -> None:
    for name in ("len", "print", "range", "dict", "append", "get", "join"):
        assert is_global_noise(name), name
    assert name in GLOBAL_RELATION_BLACKLIST


def test_global_noise_lets_business_names_through() -> None:
    for name in ("parse_jwt", "charge", "verify_token", "expand_one"):
        assert not is_global_noise(name), name


# ── Import parsing → stub modules ───────────────────────────────────────────
def test_plain_import_and_alias_resolve_to_root() -> None:
    stubs = parse_stub_modules("import numpy as np\nimport os\n")
    assert "numpy" in stubs.roots and "os" in stubs.roots
    # both the alias and the canonical name resolve to the root
    assert stubs.root_of("np.array") == "numpy"
    assert stubs.root_of("numpy.linalg.norm") == "numpy"
    assert stubs.root_of("os.path.join") == "os"


def test_third_party_excludes_stdlib() -> None:
    stubs = parse_stub_modules("import os\nimport numpy\nimport json\nimport requests\n")
    assert stubs.third_party == {"numpy", "requests"}
    # stdlib is still a stub (we don't expand it) but not "third-party"
    assert stubs.is_stub_call("os.getcwd")
    assert not stubs.is_third_party_call("os.getcwd")
    assert stubs.is_third_party_call("requests.get")


def test_from_import_binds_bare_name() -> None:
    stubs = parse_stub_modules("from numpy import array as arr\nfrom collections import OrderedDict\n")
    assert stubs.root_of("arr") == "numpy"            # aliased bare binding
    assert stubs.root_of("OrderedDict") == "numpy" or stubs.root_of("OrderedDict") == "collections"
    assert stubs.is_third_party_call("arr")
    assert stubs.is_stub_call("OrderedDict")


def test_relative_imports_are_not_stubs() -> None:
    # Relative imports are this package's own code — must remain traceable.
    stubs = parse_stub_modules("from . import sibling\nfrom .util import helper\n")
    assert stubs.is_stub_call("sibling") is False
    assert stubs.is_stub_call("helper") is False
    assert stubs.roots == set()


def test_unknown_symbol_is_not_a_stub() -> None:
    stubs = parse_stub_modules("import numpy as np\n")
    assert not stubs.is_stub_call("my_local_func")
    assert stubs.root_of("my_local_func") is None


def test_syntax_error_falls_back_to_line_scan() -> None:
    # A half-written file still yields whatever imports parsed line-by-line.
    src = "import requests\ndef broken(:\n    pass\n"
    stubs = parse_stub_modules(src)
    assert "requests" in stubs.roots


# ── Stub call-name extraction (guards source-regex recovery) ────────────────
def test_stub_call_names_picks_third_party_methods() -> None:
    src = "import numpy as np\n\ndef f(x):\n    return np.dot(x, x) + np.sum(x)\n"
    stubs = parse_stub_modules(src)
    assert stub_call_names(src, stubs) == {"dot", "sum"}


def test_stub_call_names_ignores_internal_receivers() -> None:
    # self.method() / worker.expand_one() are not stub calls — keep them traceable.
    src = ("import numpy as np\n\ndef f(self, worker):\n"
           "    self.helper()\n    worker.expand_one()\n    np.array([1])\n")
    names = stub_call_names(src, parse_stub_modules(src))
    assert "array" in names
    assert "helper" not in names and "expand_one" not in names


def test_stub_call_names_includes_from_bindings() -> None:
    src = "from numpy import dot\n\ndef f(x):\n    return dot(x, x)\n"
    assert "dot" in stub_call_names(src, parse_stub_modules(src))


def test_externals_only_drops_first_party_packages() -> None:
    # `codemap` is the project's own package — it imports like a library but is
    # internal and must not be stubbed; numpy/os remain external.
    src = "import numpy as np\nimport os\nfrom codemap.prompts import build_window\n"
    stubs = parse_stub_modules(src).externals_only({"codemap", "src", "tests"})
    assert "numpy" in stubs.roots and "os" in stubs.roots
    assert "codemap" not in stubs.roots
    # the first-party binding is no longer treated as a stub call → stays traceable
    assert stubs.root_of("build_window") is None
    assert "build_window" not in stub_call_names(src, stubs)
    assert "dot" not in stub_call_names("np.dot(x)", stubs) or stubs.root_of("np") == "numpy"


# ── Boundary classification ─────────────────────────────────────────────────
def test_classify_boundary_sink_vs_dual() -> None:
    assert classify_boundary(0) == SINK    # absorbs material → terminal
    assert classify_boundary(3) == DUAL    # forwards material → keep tracing
    # Defensive default: an unindexed external symbol (count 0) is a Sink.
    assert classify_boundary(0) == SINK
