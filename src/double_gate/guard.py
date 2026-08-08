"""Five-layer anti-hallucination guard for AI-generated review content.

This module verifies that the artifacts (files, API endpoints, imports, code
symbols and data fields) cited by an LLM review actually exist in a codebase.
It is deliberately dependency-free: only the Python standard library is used,
and searching works with or without the ``ripgrep`` binary installed.

Layer overview
--------------

* L1 :func:`extract_artifacts`         -- pull artifact references out of text
* L2 :func:`scan_artifact`             -- existence scan against one or more roots
* L3 :func:`score_artifacts`           -- aggregate verdicts into a risk score
* L4 :func:`cross_model_correlation`   -- detect shared hallucinations across models
* L5 :func:`generate_verification_prompt` / :func:`build_verification_prompt`
                                        -- prompts that ask a model to re-verify

Everything is driven by :func:`run_hallucination_guard`, which returns a
JSON-serialisable report. :class:`GuardReport` together with :func:`guard` and
:func:`inject_guard_into_report` provide the integration surface expected by
the jury orchestrator, and :func:`scan` is the convenience entry point used by
the CLI.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence

__all__ = [
    "ArtifactType",
    "Verdict",
    "Artifact",
    "ScanResult",
    "GuardReport",
    "extract_artifacts",
    "scan_artifact",
    "score_artifacts",
    "cross_model_correlation",
    "generate_verification_prompt",
    "build_verification_prompt",
    "run_hallucination_guard",
    "guard",
    "inject_guard_into_report",
    "scan",
]


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------


class ArtifactType(Enum):
    """The kind of codebase artifact a review may cite."""

    FILE = "FILE"
    API = "API"
    IMPORT = "IMPORT"
    CODE_SYMBOL = "CODE_SYMBOL"
    FIELD = "FIELD"


class Verdict(Enum):
    """Layer-2 outcome for a single artifact scan."""

    VERIFIED = "VERIFIED"
    SUSPICIOUS = "SUSPICIOUS"
    LIKELY_HALLUCINATION = "LIKELY_HALLUCINATION"
    UNVERIFIABLE = "UNVERIFIABLE"


@dataclass
class Artifact:
    """A single artifact reference extracted from review text."""

    name: str
    type: ArtifactType
    context: str = ""
    line: int = 0
    source_model: str = ""
    source_vendor: str = ""


@dataclass
class ScanResult:
    """Layer-2 outcome plus the evidence trail that produced it."""

    artifact: Artifact
    verdict: Verdict
    confidence: float = 0.0
    evidence: str = ""
    search_log: List[str] = field(default_factory=list)


@dataclass
class GuardReport:
    """Full five-layer report, mirroring :func:`run_hallucination_guard`."""

    summary: Dict[str, Any]
    layer_1_extraction: List[Dict[str, Any]]
    layer_3_scoring: Dict[str, Any]
    layer_4_cross_model: Dict[str, Any]
    layer_5_feedback_prompts: Dict[str, str]
    detailed_results: List[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "summary": dict(self.summary),
            "layer_1_extraction": list(self.layer_1_extraction),
            "layer_3_scoring": dict(self.layer_3_scoring),
            "layer_4_cross_model": dict(self.layer_4_cross_model),
            "layer_5_feedback_prompts": dict(self.layer_5_feedback_prompts),
            "detailed_results": list(self.detailed_results),
        }


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Directories never entered during recursive walks (large, generated, or VCS).
_EXCLUDED_DIRS: frozenset = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".idea",
        ".vscode",
        "dist",
        "build",
        "htmlcov",
        "coverage",
    }
)

# rg glob excludes mirroring ``_EXCLUDED_DIRS``.
_RG_EXCLUDE_GLOBS: tuple = (
    "!**/.git/**",
    "!**/.hg/**",
    "!**/.svn/**",
    "!**/node_modules/**",
    "!**/__pycache__/**",
    "!**/.venv/**",
    "!**/venv/**",
    "!**/.tox/**",
    "!**/.mypy_cache/**",
    "!**/.pytest_cache/**",
    "!**/dist/**",
    "!**/build/**",
)

# Walk guard rails: never visit more than this many directories and never read
# files larger than this many bytes during a text-search fallback.
_MAX_WALK: int = 120000
_FILE_SIZE_CAP: int = 4 * 1024 * 1024
_MAX_MATCH_FILES: int = 50
_EVIDENCE_FILES: int = 3

# Whitelisted file extensions for layer-1 FILE extraction. A name is only
# treated as a file reference when it ends in one of these.
_WHITELISTED_FILE_EXTS: frozenset = frozenset(
    {
        "py", "pyi", "pyw", "js", "mjs", "cjs", "jsx", "ts", "tsx", "md",
        "markdown", "rst", "txt", "json", "jsonl", "yaml", "yml", "toml",
        "ini", "cfg", "conf", "xml", "html", "htm", "css", "scss", "sass",
        "less", "sh", "bash", "zsh", "bat", "ps1", "db", "duckdb", "sqlite",
        "sqlite3", "sql", "csv", "tsv", "parquet", "pkl", "pickle", "env",
        "lock", "go", "rs", "java", "kt", "scala", "rb", "php", "c", "h",
        "cpp", "hpp", "cc", "cs", "swift", "pl", "pm", "r", "lua", "jl",
        "proto", "graphql", "vue", "svelte", "ipynb", "pdf", "svg", "png",
        "jpg", "jpeg", "gif", "webp", "ico", "woff", "woff2", "ttf", "eot",
        "map", "gradle", "properties",
    }
)

# Python keywords that must never be classified as data fields or symbols.
_PY_KEYWORDS: frozenset = frozenset(
    {
        "False", "None", "True", "and", "as", "assert", "async", "await",
        "break", "class", "continue", "def", "del", "elif", "else", "except",
        "finally", "for", "from", "global", "if", "import", "in", "is",
        "lambda", "nonlocal", "not", "or", "pass", "raise", "return", "try",
        "while", "with", "yield",
    }
)

# Prose words that commonly precede a parenthesis and would otherwise be
# mistaken for code-symbol references. Also drops common stdlib/builtin names
# that are not defined inside the scanned codebase.
_SYMBOL_STOPWORDS: frozenset = frozenset(
    {
        "if", "in", "is", "to", "of", "for", "and", "or", "not", "from",
        "with", "by", "as", "at", "on", "we", "the", "it", "be", "are", "was",
        "were", "has", "have", "had", "you", "your", "our", "this", "that",
        "can", "will", "may", "must", "should", "would", "could", "etc", "eg",
        "ie", "i", "a", "an", "no", "do", "does", "did", "he", "she", "they",
        "them", "their", "his", "her", "its", "but", "so", "than", "then",
        "while", "when", "where", "which", "who", "whom", "what", "why",
        "how", "all", "any", "some", "each", "every", "both", "few", "more",
        "most", "other", "such", "only", "own", "same", "too", "very", "just",
        "also", "even", "still", "already", "often", "always", "never",
        "however", "therefore", "thus", "hence", "although", "though",
        "because", "since", "unless", "whether", "print", "len", "range",
        "str", "int", "float", "list", "dict", "set", "tuple", "open", "sum",
        "max", "min", "abs", "round", "sorted", "enumerate", "zip", "map",
        "filter", "super", "object", "type", "id", "repr", "format", "input",
        "exit", "join", "get", "set", "append", "extend", "replace", "strip",
        "split", "find", "index", "count", "insert", "pop", "remove", "sort",
        "reverse", "copy", "clear", "items", "keys", "values", "update",
        "union", "intersection", "difference", "isinstance", "hasattr",
        "getattr", "setattr", "delattr", "callable", "next", "iter", "close",
        "read", "write", "seek", "tell", "flush", "bool", "self", "cls",
    }
)

# Words that should never be treated as module/import names.
_IMPORT_STOPWORDS: frozenset = frozenset(
    {
        "the", "a", "an", "of", "for", "and", "or", "from", "with", "by",
        "to", "in", "on", "at", "as", "is", "are", "was", "were", "be", "been",
        "being", "have", "has", "had", "do", "does", "did", "will", "would",
        "can", "could", "may", "might", "should", "shall", "must", "not",
        "so", "if", "then", "when", "while", "because", "although", "since",
        "unless", "until", "whether", "nor", "both", "either", "neither",
        "such", "etc", "eg", "ie", "only", "just", "also", "even", "still",
        "already", "always", "never", "often", "usually", "however",
        "therefore", "thus", "hence", "moreover", "furthermore",
        "additionally", "conversely", "likewise", "similarly", "i", "we",
        "you", "they", "he", "she", "it", "them", "us", "me", "him", "her",
        "my", "our", "your", "their", "its", "each", "every", "any", "some",
        "all", "one", "two", "new", "old", "own", "same",
    }
)


# ---------------------------------------------------------------------------
# Layer-1: extraction
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[A-Za-z0-9_.\-@/\\~]+")
_API_METHOD_RE = re.compile(
    r"\b(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+(/[/A-Za-z0-9_.\-{}\[\]?&=]+)"
)
_API_PATH_RE = re.compile(r"(?<![A-Za-z0-9_.-])(/[/A-Za-z0-9_.\-{}\[\]?&=]+)")
_IMPORT_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(?:"
    r"import\s+([A-Za-z_][\w.]*(?:\s*,\s*[A-Za-z_][\w.]*)*)"
    r"|from\s+([A-Za-z_][\w.]*)\s+import"
    r")"
)
_SYMBOL_DEF_RE = re.compile(
    r"(?:^|[^\w.])(def|class|function)\s+([A-Za-z_]\w*)", re.MULTILINE
)
_SYMBOL_REF_RE = re.compile(r"\b([A-Za-z_]\w{1,})\s*\(")
_CAMEL_FIELD_RE = re.compile(r"\b[a-z][a-zA-Z0-9]*[A-Z][a-zA-Z0-9]*\b")
_SNAKE_FIELD_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")


def _line_context(text: str, start: int, end: int) -> tuple:
    """Return the 1-based line number and the trimmed line around ``[start:end]``."""
    line = text.count("\n", 0, start) + 1
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end == -1:
        line_end = len(text)
    return line, text[line_start:line_end].strip()


def _looks_like_file(path: str) -> bool:
    """True when ``path`` ends in a whitelisted file extension."""
    match = re.search(r"\.([A-Za-z0-9]+)$", path)
    return bool(match and match.group(1).lower() in _WHITELISTED_FILE_EXTS)


def extract_artifacts(
    text: Optional[str],
    source_model: str = "",
    source_vendor: str = "",
) -> List[Artifact]:
    """Layer 1: pull artifact references out of review text.

    The extraction is deliberately generous -- false positives are meant to be
    filtered out by layer 2. Supported artifact kinds:

    * FILE: path-like tokens ending in a whitelisted extension
    * API: HTTP-method verbs followed by a route, and bare ``/api/...`` paths
    * IMPORT: ``import x`` and ``from x import y`` statements
    * CODE_SYMBOL: ``def`` / ``class`` definitions and function-call references
    * FIELD: generic camelCase / snake_case identifiers

    ``source_model`` and ``source_vendor`` are stamped on every artifact so
    later layers can attribute each reference and detect shared hallucinations.
    """
    if not text:
        return []
    artifacts: List[Artifact] = []
    seen: set = set()

    def add(atype: ArtifactType, name: str, start: int, end: int) -> None:
        key = (atype.value, name)
        if key in seen:
            return
        seen.add(key)
        line, context = _line_context(text, start, end)
        artifacts.append(
            Artifact(
                name=name,
                type=atype,
                context=context,
                line=line,
                source_model=source_model,
                source_vendor=source_vendor,
            )
        )

    # --- FILE -----------------------------------------------------------
    for match in _TOKEN_RE.finditer(text):
        token = match.group().rstrip(".,;:!?)\"'")
        ext_match = re.search(r"\.([A-Za-z0-9]+)$", token)
        if not ext_match:
            continue
        if ext_match.group(1).lower() not in _WHITELISTED_FILE_EXTS:
            continue
        base = token[: ext_match.start()]
        if not base or base in (".", ".."):
            continue
        if "://" in token:
            continue  # URL
        if match.start() > 0 and text[match.start() - 1] == ":":
            continue  # data URI / scheme
        if re.fullmatch(r"[\d.]+", base):
            continue  # version number, e.g. 1.2.3
        add(ArtifactType.FILE, token, match.start(), match.end())

    # --- API --------------------------------------------------------------
    for match in _API_METHOD_RE.finditer(text):
        path = match.group(2).rstrip("/.")
        if path and not _looks_like_file(path):
            add(ArtifactType.API, path, match.start(), match.end())

    for match in _API_PATH_RE.finditer(text):
        path = match.group(1).rstrip("/.")
        if not path or _looks_like_file(path):
            continue
        if re.search(r"api", path, re.IGNORECASE) is None:
            continue
        add(ArtifactType.API, path, match.start(), match.end())

    # --- IMPORT ------------------------------------------------------------
    for match in _IMPORT_RE.finditer(text):
        module_list = match.group(1)
        if module_list:
            for module in module_list.split(","):
                module = module.strip()
                if len(module) >= 2 and module not in _IMPORT_STOPWORDS:
                    add(ArtifactType.IMPORT, module, match.start(), match.end())
        elif match.group(2):
            module = match.group(2).strip()
            if len(module) >= 2 and module not in _IMPORT_STOPWORDS:
                add(ArtifactType.IMPORT, module, match.start(), match.end())

    # --- CODE_SYMBOL (definitions) -----------------------------------------
    for match in _SYMBOL_DEF_RE.finditer(text):
        name = match.group(2)
        if name not in _PY_KEYWORDS:
            add(ArtifactType.CODE_SYMBOL, name, match.start(), match.end())

    # --- CODE_SYMBOL (references, function-call shaped) ----------------------
    for match in _SYMBOL_REF_RE.finditer(text):
        name = match.group(1)
        if name in _SYMBOL_STOPWORDS or name in _PY_KEYWORDS:
            continue
        add(ArtifactType.CODE_SYMBOL, name, match.start(), match.end())

    # --- FIELD (camelCase / snake_case) ------------------------------------
    for match in _CAMEL_FIELD_RE.finditer(text):
        if match.group(0) not in _PY_KEYWORDS:
            add(ArtifactType.FIELD, match.group(0), match.start(), match.end())

    for match in _SNAKE_FIELD_RE.finditer(text):
        if match.group(0) not in _PY_KEYWORDS:
            add(ArtifactType.FIELD, match.group(0), match.start(), match.end())

    return artifacts


# ---------------------------------------------------------------------------
# Layer-2: existence scanning
# ---------------------------------------------------------------------------


def _normalize_roots(roots: Optional[Any]) -> List[str]:
    """Normalise ``codebase_roots`` to a deduplicated list of absolute paths."""
    if roots is None:
        roots = [os.getcwd()]
    if isinstance(roots, (str, bytes)):
        roots = [roots]
    cleaned: List[str] = []
    for root in roots or []:
        try:
            path = os.fspath(root)
        except TypeError:
            continue
        if not path:
            continue
        try:
            absolute = os.path.abspath(os.path.expanduser(path))
        except Exception:
            continue
        if absolute not in cleaned:
            cleaned.append(absolute)
    cleaned.sort(key=lambda p: p.count(os.sep))
    deduped: List[str] = []
    for path in cleaned:
        if any(path.startswith(other + os.sep) for other in deduped):
            continue
        deduped.append(path)
    if not deduped:
        deduped = [os.getcwd()]
    return deduped


def _is_within_roots(path: str, roots: Sequence[str]) -> bool:
    """Return whether ``path`` resolves below one of the approved roots.

    Artifact names are generated by a model and must be treated as untrusted
    input.  Resolving symlinks here prevents a seemingly harmless relative path
    (or a symlink inside the repository) from turning the guard into a probe of
    arbitrary host files.
    """
    try:
        target = os.path.realpath(path)
    except OSError:
        return False
    for root in roots:
        try:
            resolved_root = os.path.realpath(root)
            if os.path.commonpath((target, resolved_root)) == resolved_root:
                return True
        except (OSError, ValueError):
            continue
    return False


def _walk_files(roots: Sequence[str], max_walk: int = _MAX_WALK):
    """Yield ``(dirpath, filenames)`` across roots, honouring exclusions."""
    visited = set()
    count = 0
    for root in roots:
        if root in visited:
            continue
        visited.add(root)
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS]
            count += 1
            if count > max_walk:
                return
            yield dirpath, filenames


def _find_file_by_basename(basename: str, roots: Sequence[str]) -> Optional[str]:
    """Recursively locate a file by its basename under any of ``roots``.

    This is the fallback that lets data assets such as ``*.db``, ``*.duckdb``
    and ``*.json`` referenced by a bare name be verified no matter which
    sub-directory they live in.
    """
    name = os.path.basename(basename.rstrip("/\\"))
    if not name:
        return None
    for dirpath, filenames in _walk_files(roots):
        if name in filenames:
            return os.path.join(dirpath, name)
    return None


def _rg_available() -> bool:
    """True when the ``rg`` binary can be invoked."""
    try:
        proc = subprocess.run(
            ["rg", "--version"], capture_output=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _rg_search(pattern: str, roots: Sequence[str]) -> List[str]:
    """Search with ``rg -l``; returns matching file paths (may be empty)."""
    cmd = ["rg", "-l", "--hidden", "--no-messages", "--no-config", "-e", pattern]
    for glob in _RG_EXCLUDE_GLOBS:
        cmd += ["-g", glob]
    cmd += list(roots)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _read_searchable(path: str) -> Optional[str]:
    """Read a small, text-like file for the pure-Python search fallback."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size <= 0 or size > _FILE_SIZE_CAP:
        return None
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    if b"\x00" in data:
        return None  # binary
    try:
        return data.decode("utf-8", errors="replace")
    except Exception:
        return None


def _text_search(pattern: str, roots: Sequence[str]) -> List[str]:
    """Pure-Python fallback: regex search over every file under ``roots``."""
    compiled = re.compile(pattern)
    matches: List[str] = []
    for dirpath, filenames in _walk_files(roots):
        for filename in filenames:
            path = os.path.join(dirpath, filename)
            content = _read_searchable(path)
            if content is None:
                continue
            if compiled.search(content):
                matches.append(path)
                if len(matches) >= _MAX_MATCH_FILES:
                    return matches
    return matches


def _search_source(pattern: str, roots: Sequence[str]) -> tuple:
    """Search ``pattern`` via rg when available, else via the stdlib walk."""
    try:
        if _rg_available():
            hits = _rg_search(pattern, roots)
            if hits is not None:
                return hits, "rg"
    except Exception:
        pass
    return _text_search(pattern, roots), "text-walk"


def _route_pattern(name: str) -> str:
    """Build a search regex from a route, treating placeholders as wildcards."""
    name = name.rstrip("/")
    tokens = re.split(r"(\{[^}]*\}|\[[^\]]*\])", name)
    parts: List[str] = []
    for token in tokens:
        if re.fullmatch(r"\{[^}]*\}|\[[^\]]*\]", token):
            parts.append(".*")
        else:
            parts.append(re.escape(token))
    return "".join(parts)


def _import_pattern(name: str) -> str:
    """Build a search regex that matches ``import x`` and ``from x import``."""
    escaped = re.escape(name)
    variants = [rf"(?:import|from)\s+{escaped}\b"]
    parts = name.split(".")
    if len(parts) >= 2:
        joined = re.escape(".".join(parts[:-1]))
        variants.append(rf"from\s+{joined}\s+import")
    return "|".join(f"(?:{variant})" for variant in variants)


def _scan_file(
    artifact: Artifact, name: str, roots: Sequence[str], log: List[str]
) -> ScanResult:
    if os.path.isabs(name):
        if not _is_within_roots(name, roots):
            log.append("absolute-path-outside-roots")
            return ScanResult(
                artifact,
                Verdict.UNVERIFIABLE,
                0.0,
                "absolute file path is outside the approved scanned roots",
                log,
            )
        if os.path.isfile(name):
            log.append("direct-abs-hit")
            return ScanResult(artifact, Verdict.VERIFIED, 0.95, f"exists: {name}", log)
        log.append("direct-abs-miss")
    else:
        attempted_escape = False
        for root in roots:
            candidate = os.path.realpath(os.path.join(root, name))
            if not _is_within_roots(candidate, [root]):
                attempted_escape = True
                continue
            if os.path.isfile(candidate):
                log.append("direct-relative-hit")
                return ScanResult(artifact, Verdict.VERIFIED, 0.95, f"exists: {candidate}", log)
        log.append(
            "relative-path-outside-roots" if attempted_escape else "direct-relative-miss"
        )
        if attempted_escape:
            return ScanResult(
                artifact,
                Verdict.UNVERIFIABLE,
                0.0,
                "relative file path escapes the approved scanned roots",
                log,
            )

    # A bare filename can reasonably be located anywhere under the selected
    # repository.  Do not apply this fallback to a path-qualified reference:
    # `src/a.py` and `examples/a.py` are materially different claims.
    if os.path.basename(name) == name and "\\" not in name:
        found = _find_file_by_basename(name, roots)
        if found is not None:
            log.append("basename-walk-hit")
            return ScanResult(artifact, Verdict.VERIFIED, 0.7, f"found by basename: {found}", log)
    log.append("basename-walk-miss")
    return ScanResult(
        artifact,
        Verdict.LIKELY_HALLUCINATION,
        0.9,
        "file not found in any scanned root",
        log,
    )


def _scan_code_symbol(
    artifact: Artifact, name: str, roots: Sequence[str], log: List[str]
) -> ScanResult:
    escaped = re.escape(name)
    definition_pattern = rf"(?:^|[^\w.])(?:def|class)\s+{escaped}\b"
    def_hits, via = _search_source(definition_pattern, roots)
    log.append(f"definition-search={via}")
    if def_hits:
        log.append("definition-found")
        return ScanResult(
            artifact,
            Verdict.VERIFIED,
            0.9,
            f"definition found in: {def_hits[0]}",
            log,
        )
    ref_hits, via2 = _search_source(rf"\b{escaped}\b", roots)
    log.append(f"reference-search={via2}")
    if ref_hits:
        log.append("reference-only")
        return ScanResult(
            artifact,
            Verdict.SUSPICIOUS,
            0.5,
            "referenced but no definition found in: "
            + "; ".join(ref_hits[:_EVIDENCE_FILES]),
            log,
        )
    log.append("no-match")
    return ScanResult(
        artifact,
        Verdict.LIKELY_HALLUCINATION,
        0.9,
        "symbol not found in any scanned root",
        log,
    )


def scan_artifact(artifact: Artifact, codebase_roots: Optional[Any] = None) -> ScanResult:
    """Layer 2: verify a single artifact against one or more codebase roots.

    ``codebase_roots`` defaults to :func:`os.getcwd` -- the current working
    directory -- and is never hard-coded. Each artifact type is scanned with a
    type-appropriate strategy, and a pure-Python fallback keeps the guard
    working on machines without ``ripgrep``.
    """
    roots = _normalize_roots(codebase_roots)
    name = (artifact.name or "").strip()
    log: List[str] = [f"roots={','.join(roots)}"]
    if not name:
        return ScanResult(artifact, Verdict.UNVERIFIABLE, 0.0, "empty artifact name", log)

    if artifact.type == ArtifactType.FILE:
        return _scan_file(artifact, name, roots, log)
    if artifact.type == ArtifactType.CODE_SYMBOL:
        return _scan_code_symbol(artifact, name, roots, log)

    if artifact.type == ArtifactType.API:
        pattern = _route_pattern(name)
    elif artifact.type == ArtifactType.IMPORT:
        pattern = _import_pattern(name)
    else:  # FIELD
        pattern = rf"\b{re.escape(name)}\b"

    hits, via = _search_source(pattern, roots)
    log.append(f"search={via}")
    if hits:
        log.append(f"hits={len(hits)}")
        return ScanResult(
            artifact,
            Verdict.VERIFIED,
            0.9,
            "matched in: " + "; ".join(hits[:_EVIDENCE_FILES]),
            log,
        )
    log.append("no-match")
    return ScanResult(
        artifact,
        Verdict.LIKELY_HALLUCINATION,
        0.9,
        "pattern not found in any scanned root",
        log,
    )


# ---------------------------------------------------------------------------
# Layer-3: scoring
# ---------------------------------------------------------------------------


def score_artifacts(results: Sequence[ScanResult]) -> Dict[str, Any]:
    """Layer 3: aggregate verdicts into counts, a rate and an overall risk.

    Risk rules: any LIKELY_HALLUCINATION (or a rate above 15%) is ``high``;
    a rate above 5% is ``medium``; otherwise ``low``.
    """
    total = len(results)
    verified = sum(1 for r in results if r.verdict == Verdict.VERIFIED)
    suspicious = sum(1 for r in results if r.verdict == Verdict.SUSPICIOUS)
    hallucination = sum(1 for r in results if r.verdict == Verdict.LIKELY_HALLUCINATION)
    unverifiable = sum(1 for r in results if r.verdict == Verdict.UNVERIFIABLE)
    rate = round(hallucination / total, 4) if total else 0.0

    if hallucination > 0 or rate > 0.15:
        risk = "high"
    elif rate > 0.05:
        risk = "medium"
    else:
        risk = "low"

    return {
        "total": total,
        "verified": verified,
        "suspicious": suspicious,
        "hallucination": hallucination,
        "unverifiable": unverifiable,
        "hallucination_rate": rate,
        "overall_risk": risk,
    }


# ---------------------------------------------------------------------------
# Layer-4: cross-model correlation
# ---------------------------------------------------------------------------


def cross_model_correlation(
    all_artifacts: Dict[str, Sequence[ScanResult]]
) -> Dict[str, Any]:
    """Layer 4: detect shared hallucinations across independent models.

    When the same artifact is cited by at least two distinct models and every
    scan judged it LIKELY_HALLUCINATION, the artifact is flagged as a shared
    hallucination. Consistent errors across independent models are treated as a
    stronger signal than a single model erring alone.
    """
    buckets: Dict[tuple, List[tuple]] = {}
    for model_key, results in (all_artifacts or {}).items():
        for result in results or []:
            key = (result.artifact.name, result.artifact.type.value)
            buckets.setdefault(key, []).append((model_key, result))

    warnings: List[Dict[str, Any]] = []
    for (name, kind), entries in buckets.items():
        if len(entries) < 2:
            continue
        models = {model_key for model_key, _ in entries}
        if len(models) < 2:
            continue
        if all(r.verdict == Verdict.LIKELY_HALLUCINATION for _, r in entries):
            vendors = sorted(
                {
                    result.artifact.source_vendor
                    for _, result in entries
                    if result.artifact.source_vendor
                }
            )
            cross_provider = len(vendors) >= 2
            warnings.append(
                {
                    "id": hashlib.sha256(
                        f"{kind}:{name}".encode("utf-8")
                    ).hexdigest()[:12],
                    "artifact": name,
                    "type": kind,
                    "models": sorted(models),
                    "vendors": vendors,
                    "independence": "cross-provider" if cross_provider else "same-provider-or-unknown",
                    "hallucination_count": len(entries),
                    "severity": "high",
                    "warning": (
                        "multiple reviewer responses cited the same artifact that could not "
                        "be verified in the codebase -- likely a shared hallucination"
                    ),
                }
            )

    warnings.sort(key=lambda w: (w["artifact"], w["type"]))
    return {
        "models": sorted((all_artifacts or {}).keys()),
        "shared_hallucination_warnings": warnings,
        "shared_hallucination_count": len(warnings),
    }


# ---------------------------------------------------------------------------
# Layer-5: feedback prompts
# ---------------------------------------------------------------------------


def generate_verification_prompt(scan_results: Sequence[ScanResult]) -> str:
    """Layer 5: build a re-verification prompt for every likely hallucination.

    Returns an empty string when nothing needs re-verification.
    """
    likely = [r for r in scan_results if r.verdict == Verdict.LIKELY_HALLUCINATION]
    if not likely:
        return ""
    lines = [
        "The following artifacts were cited in the review but could not be located "
        "in the codebase. Please confirm whether each one genuinely exists, or "
        "correct the reference.",
        "",
    ]
    for result in likely:
        lines.append(f"- [{result.artifact.type.value}] {result.artifact.name}")
    lines.append("")
    lines.append(
        "If an artifact exists under a different name or location, provide the "
        "correct name and path."
    )
    return "\n".join(lines)


def build_verification_prompt(verification: Any, original_content: Any) -> str:
    """Layer 5: compose the second-round prompt sent back to the original model.

    ``verification`` is the confirmation collected after the first prompt and
    ``original_content`` is the review text that triggered the scan.
    """
    if not isinstance(verification, str):
        try:
            import json

            verification = json.dumps(verification, ensure_ascii=False)
        except Exception:
            verification = str(verification)
    return "\n".join(
        [
            "You are verifying a prior AI review for accuracy.",
            "",
            "=== ORIGINAL REVIEW CONTENT ===",
            str(original_content) if original_content else "(no content)",
            "",
            "=== VERIFICATION RESPONSE ===",
            verification if verification else "(no response)",
            "",
            "Review the original content against the verification response above. "
            "Correct any references that were confirmed as nonexistent and return "
            "a revised, accurate version of the review.",
        ]
    )


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _artifact_to_dict(artifact: Artifact) -> Dict[str, Any]:
    return {
        "name": artifact.name,
        "type": artifact.type.value,
        "context": artifact.context,
        "line": artifact.line,
        "source_model": artifact.source_model,
        "source_vendor": artifact.source_vendor,
    }


def _scan_result_to_dict(result: ScanResult) -> Dict[str, Any]:
    return {
        "artifact": _artifact_to_dict(result.artifact),
        "verdict": result.verdict.value,
        "confidence": result.confidence,
        "evidence": result.evidence,
        "search_log": list(result.search_log),
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_hallucination_guard(
    model_texts: Dict[str, Any],
    codebase_roots: Optional[Any] = None,
) -> Dict[str, Any]:
    """Run the full five-layer pipeline over one or more models' review texts.

    ``model_texts`` maps a reviewer key to review text.  Values may also be a
    mapping with ``content``/``text`` and optional ``vendor`` keys. The returned report is
    JSON-serialisable and contains ``summary``, ``layer_1_extraction``,
    ``layer_3_scoring``, ``layer_4_cross_model``, ``layer_5_feedback_prompts``
    and ``detailed_results``.
    """
    per_model: Dict[str, List[ScanResult]] = {}
    layer_1: List[Dict[str, Any]] = []
    for model_key, value in (model_texts or {}).items():
        source_vendor = ""
        if isinstance(value, dict):
            text = value.get("content", value.get("text", ""))
            source_vendor = str(value.get("vendor") or "")
        else:
            text = value
        text = text if isinstance(text, str) else str(text or "")
        model_key = str(model_key)
        artifacts = extract_artifacts(
            text, source_model=model_key, source_vendor=source_vendor
        )
        results = [scan_artifact(artifact, codebase_roots) for artifact in artifacts]
        per_model[model_key] = results
        layer_1.append(
            {
                "model": model_key,
                "vendor": source_vendor,
                "artifact_count": len(artifacts),
                "artifacts": [_artifact_to_dict(a) for a in artifacts],
            }
        )

    flat_results = [result for results in per_model.values() for result in results]
    scoring = score_artifacts(flat_results)
    cross_model = cross_model_correlation(per_model)
    feedback: Dict[str, str] = {}
    for model_key, results in per_model.items():
        prompt = generate_verification_prompt(results)
        if prompt:
            feedback[model_key] = prompt

    summary = {
        "models_checked": len(per_model),
        "total_artifacts": scoring["total"],
        "verified": scoring["verified"],
        "suspicious": scoring["suspicious"],
        "hallucination": scoring["hallucination"],
        "unverifiable": scoring["unverifiable"],
        "hallucination_rate": scoring["hallucination_rate"],
        "overall_hallucination_risk": scoring["overall_risk"],
        "guard_score": round(1.0 - scoring["hallucination_rate"], 4),
        "shared_hallucinations": cross_model["shared_hallucination_count"],
    }

    return {
        "summary": summary,
        "layer_1_extraction": layer_1,
        "layer_3_scoring": scoring,
        "layer_4_cross_model": cross_model,
        "layer_5_feedback_prompts": feedback,
        "detailed_results": [_scan_result_to_dict(r) for r in flat_results],
    }


def guard(adjudication: Any, codebase_root: Optional[Any] = None) -> GuardReport:
    """Run the hallucination guard against an adjudication record.

    ``adjudication`` may be a dict carrying ``model_texts``, ``content`` or a
    ``findings`` list, or a plain string. Returns a :class:`GuardReport`
    suitable for :func:`inject_guard_into_report`.
    """
    model_texts: Dict[str, str] = {}
    if isinstance(adjudication, dict):
        provided = adjudication.get("model_texts")
        if isinstance(provided, dict):
            model_texts = {str(k): v for k, v in provided.items()}
        content = adjudication.get("content")
        if content and not model_texts:
            model_texts["adjudicator"] = str(content)
        if not model_texts:
            findings = adjudication.get("findings")
            if isinstance(findings, list):
                joined = "\n".join(
                    str(item.get("content") or item.get("text") or "")
                    for item in findings
                    if isinstance(item, dict)
                )
                if joined.strip():
                    model_texts["adjudicator"] = joined
    elif isinstance(adjudication, str):
        model_texts["adjudicator"] = adjudication

    result = run_hallucination_guard(model_texts, codebase_roots=codebase_root)
    return GuardReport(
        summary=result["summary"],
        layer_1_extraction=result["layer_1_extraction"],
        layer_3_scoring=result["layer_3_scoring"],
        layer_4_cross_model=result["layer_4_cross_model"],
        layer_5_feedback_prompts=result["layer_5_feedback_prompts"],
        detailed_results=result["detailed_results"],
    )


def inject_guard_into_report(adjudication: Any, guard_report: GuardReport) -> Dict[str, Any]:
    """Merge a :class:`GuardReport` into an adjudication record.

    The merged copy carries the full guard payload plus the flat keys that the
    jury orchestrator and the quality gates expect (``guard_score``,
    ``overall_hallucination_risk`` and a ``hallucination_guard`` block with a
    ``risk_level``).
    """
    payload = (
        guard_report.to_dict()
        if isinstance(guard_report, GuardReport)
        else dict(guard_report)
    )
    merged = dict(adjudication or {})
    merged["guard"] = payload
    scoring = payload.get("layer_3_scoring", {})
    summary = payload.get("summary", {})
    risk = summary.get("overall_hallucination_risk", scoring.get("overall_risk", "low"))
    merged["guard_score"] = summary.get("guard_score", 1.0)
    merged["verified_artifacts"] = scoring.get("verified", 0)
    merged["suspicious_artifacts"] = scoring.get("suspicious", 0)
    merged["hallucinated_artifacts"] = scoring.get("hallucination", 0)
    merged["overall_hallucination_risk"] = risk
    merged["hallucination_guard"] = {"risk_level": risk, "report": payload}
    return merged


def scan(content: str, root: Optional[str] = ".") -> Dict[str, Any]:
    """Run the full pipeline on a single text against one root (CLI entry point)."""
    return run_hallucination_guard({"content": content}, codebase_roots=root)
