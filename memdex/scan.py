"""List a project's files and extract a cheap outline from each.

The outline (top docstring/comment plus symbols with line numbers) is built
without any model call, so every indexed file has a zero-cost summary even
before an agent writes a real one.

Patterns are deliberately cheap: one regex per language construct, matched
line by line, no parser dependency. The symbol name of every pattern must be
capture group 1 -- `outline()` only reads `match.group(1)`.
"""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .project import Project, git

MAX_BYTES = 512 * 1024
MAX_SYMBOLS = 60

IGNORE_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", "out", "target", ".next", ".nuxt", ".cache", ".idea", ".vscode",
    ".gradle", ".mypy_cache", ".pytest_cache", ".tox", "coverage", ".turbo", ".memdex",
}

# Never read these: they tend to hold credentials.
SECRET_GLOBS = [
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.jks",
    "id_rsa*", "id_ed25519*", "*secret*", "*credential*", ".npmrc", ".pypirc", ".netrc",
]

BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".psd", ".pdf", ".zip",
    ".gz", ".7z", ".rar", ".tar", ".exe", ".dll", ".so", ".dylib", ".class", ".jar",
    ".pyc", ".woff", ".woff2", ".ttf", ".otf", ".mp3", ".mp4", ".wav", ".ogg", ".mov",
    ".glb", ".gltf", ".fbx", ".blend", ".db", ".sqlite", ".bin", ".lock",
}


# Generated and huge; an outline of them is noise.
SKIP_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "cargo.lock", "poetry.lock",
    "pipfile.lock", "composer.lock", "gemfile.lock", "go.sum", "uv.lock",
}


def is_secret(relpath: str) -> bool:
    name = os.path.basename(relpath).lower()
    return any(fnmatch.fnmatch(name, g) for g in SECRET_GLOBS)


def list_files(project: Project) -> list[str]:
    """Relative paths (forward slashes) of candidate text files."""
    paths: list[str] = []
    out = None
    if project.is_git:
        out = git(["ls-files", "-z", "-co", "--exclude-standard"], project.root)
    if out is not None:
        paths = [p for p in out.split("\0") if p]
    else:
        for dirpath, dirnames, filenames in os.walk(project.root):
            dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS and not d.startswith(".")]
            rel = os.path.relpath(dirpath, project.root)
            for f in filenames:
                paths.append(f if rel == "." else f"{rel}/{f}".replace(os.sep, "/"))
    keep = []
    for p in paths:
        p = p.replace("\\", "/")
        parts = p.split("/")
        if any(part in IGNORE_DIRS for part in parts[:-1]):
            continue
        if Path(p).suffix.lower() in BINARY_EXT or parts[-1].lower() in SKIP_NAMES or is_secret(p):
            continue
        keep.append(p)
    return sorted(set(keep))


def read_text(path: str) -> Optional[tuple[bytes, str]]:
    """(raw bytes, decoded text), or None for binary/oversized files."""
    try:
        if os.path.getsize(path) > MAX_BYTES:
            return None
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    if b"\0" in raw[:8192]:
        return None
    return raw, raw.decode("utf-8", "replace")


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


@dataclass
class Outline:
    lang: str
    head: str = ""
    symbols: list[tuple[str, str, int]] = field(default_factory=list)  # (name, kind, line)
    lines: int = 0

    def render(self) -> str:
        parts = []
        if self.head:
            parts.append(self.head)
        if self.symbols:
            shown = self.symbols[:MAX_SYMBOLS]
            parts.append("; ".join(f"{k} {n}:{ln}" for n, k, ln in shown))
            if len(self.symbols) > MAX_SYMBOLS:
                parts.append(f"(+{len(self.symbols) - MAX_SYMBOLS} more symbols)")
        return "\n".join(parts)


LANG_BY_EXT = {
    ".py": "python", ".pyi": "python",
    ".js": "js", ".mjs": "js", ".cjs": "js", ".jsx": "js",
    ".ts": "js", ".tsx": "js", ".mts": "js", ".cts": "js",
    ".vue": "vue", ".svelte": "svelte",
    ".go": "go", ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin", ".kts": "kotlin",
    ".scala": "scala", ".sc": "scala",
    ".cs": "csharp",
    ".swift": "swift",
    ".dart": "dart",
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".hpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hh": "cpp", ".hxx": "cpp",
    ".rb": "ruby", ".php": "php", ".lua": "lua", ".gd": "gdscript",
    ".sh": "shell", ".bash": "shell", ".ps1": "powershell",
    ".md": "markdown", ".mdx": "markdown",
}

_ID = r"[A-Za-z_$][\w$]*"
_ANN = r"@\w+(?:\([^)]*\))?\s*"  # Java/Kotlin/Swift annotation or attribute

# C and C++ share one scanner; control-flow keywords are excluded so that
# `if (...)`, `while (...)`, `for (...)` are never mistaken for definitions.
_C_CONTROL = (
    "if|for|while|switch|case|return|else|do|try|catch|sizeof|new|delete|throw|"
    "typedef|using|namespace|template|operator|goto|break|continue|co_return|co_await"
)
CPP_PATTERNS: list[tuple[str, str]] = [
    (rf"^[ \t]*(?:typedef\s+)?(?:template\s*<[^>]*>\s*)?(?:class|struct|union)\s+({_ID})\s*(?:[:{{;]|$)", "type"),
    (rf"^[ \t]*enum(?:\s+(?:class|struct))?\s+({_ID})", "type"),
    (rf"^[ \t]*#define\s+({_ID})", "macro"),
    (rf"^[ \t]*namespace\s+({_ID})", "mod"),
    (rf"^[ \t]*(?:template\s*<[^>]*>\s*)?(?!(?:{_C_CONTROL})\b)"
     rf"(?:[\w\*&:<>,]+\s+)+\**({_ID})\s*\([^;]*\)\s*(?:const\s*)?(?:noexcept\s*)?(?:\{{|$)", "fn"),
]

PATTERNS: dict[str, list[tuple[str, str]]] = {
    "js": [
        (rf"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\*?\s+({_ID})", "fn"),
        (rf"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+({_ID})", "class"),
        (rf"^\s*(?:export\s+)?(?:declare\s+)?(?:interface|type|enum)\s+({_ID})", "type"),
        (rf"^\s*(?:export\s+)?(?:const|let|var)\s+({_ID})\s*=\s*(?:async\s+)?(?:function|\([^)]*\)\s*(?::[^=]+)?=>|{_ID}\s*=>)", "fn"),
        (rf"^\s*export\s+(?:const|let|var)\s+({_ID})", "const"),
    ],
    "go": [
        (r"^func\s+(?:\([^)]*\)\s*)?(\w+)", "fn"),
        (r"^type\s+(\w+)", "type"),
    ],
    "rust": [
        (r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+(\w+)", "fn"),
        (r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|type|union)\s+(\w+)", "type"),
        (r"^\s*impl(?:<[^>]*>)?\s+([\w:]+(?:\s+for\s+\w+)?)", "impl"),
        (r"^\s*(?:pub\s+)?mod\s+(\w+)", "mod"),
    ],
    "java": [
        (rf"^\s*(?:{_ANN})*@interface\s+({_ID})", "class"),
        (rf"^\s*(?:{_ANN})*(?:(?:public|protected|private|static|final|abstract|sealed|non-sealed|strictfp)\s+)*"
         rf"(?:class|interface|enum|record)\s+({_ID})", "class"),
        (rf"^\s*(?:{_ANN})*(?:(?:public|protected|private|static|final|abstract|synchronized|native|default|strictfp)\s+)+"
         rf"(?:<[^>]*>\s*)?[\w$.<>\[\],?]+\s+({_ID})\s*\(", "method"),
        (rf"^\s*(?:{_ANN})*(?:public|protected|private)\s+({_ID})\s*\(", "method"),
        (rf"^\s{{2,}}(?!(?:if|for|while|switch|catch|return|new|throw|else|do|try)\b)"
         rf"(?:<[^>]*>\s*)?[\w$.<>\[\],?]+\s+({_ID})\s*\([^;]*\)\s*(?:throws\s+[\w.,\s]+)?\{{", "method"),
        (rf"^\s*(?!(?:if|for|while|switch|catch|return|new|throw|else|do|try|assert|yield)\b)"
         rf"(?:<[^>]*>\s*)?[\w$.<>\[\],?]+\s+({_ID})\s*\([^)]*\)\s*(?:throws\s+[\w.,\s]+)?;", "method"),
    ],
    "kotlin": [
        (rf"^\s*(?:(?:public|private|protected|internal|open|abstract|sealed|data|value|enum|annotation|"
         rf"inline|inner|companion|expect|actual|fun)\s+)*(?:class|interface|object)\s+({_ID})", "class"),
        (rf"^\s*(?:(?:public|private|protected|internal|open|abstract|override|suspend|inline|operator|infix|"
         rf"tailrec|external|expect|actual|const|noinline|crossinline)\s+)*fun\s+"
         rf"(?:<[^>]*>\s*)?(?:[\w.<>,? ]+\.\s*)?({_ID})\s*\(", "fn"),
        (rf"^\s*(?:(?:public|private|protected|internal|override|const|lateinit|external)\s+)+"
         rf"(?:val|var)\s+({_ID})", "prop"),
        (rf"^[ \t]{{0,4}}(?:val|var)\s+({_ID})", "prop"),
        (rf"^\s*(?:(?:public|private|protected|internal)\s+)?typealias\s+({_ID})", "type"),
    ],
    "scala": [
        (rf"^\s*(?:(?:final|sealed|abstract|private|protected|case|implicit)\s+)*"
         rf"(?:class|trait|object|enum)\s+({_ID})", "class"),
        (rf"^\s*(?:(?:private|protected|final|override|implicit|inline|transparent)\s+)*def\s+({_ID})", "fn"),
        (rf"^\s*(?:(?:private|protected|final|override|implicit|lazy)\s+)*val\s+({_ID})", "prop"),
    ],
    "csharp": [
        (rf"^\s*(?:\[[^\]]*\]\s*)*(?:(?:public|private|protected|internal|static|sealed|abstract|partial|"
         rf"readonly|unsafe|new|ref)\s+)*(?:class|interface|enum|record(?:\s+struct)?|struct|delegate)\s+({_ID})",
         "class"),
        (rf"^\s*(?:\[[^\]]*\]\s*)*(?:(?:public|private|protected|internal|static|virtual|override|abstract|"
         rf"sealed|async|extern|unsafe|partial|new|readonly)\s+)+[\w<>\[\],.?]+\s+({_ID})\s*(?:<[^>]*>)?\s*\(",
         "method"),
        (rf"^\s*(?:(?:public|private|protected|internal|static)\s+)+({_ID})\s*\(", "method"),
        (rf"^\s{{1,8}}(?!(?:return|new|throw|await|using|foreach|for|while|if|switch|catch|lock|nameof|"
         rf"sizeof|typeof|base|this|else|do|goto|yield)\b)(?:[\w<>\[\],.?]+\s+)+({_ID})\s*\(", "method"),
        (rf"^\s*(?:(?:public|private|protected|internal|static|virtual|override|sealed|abstract|readonly)\s+)+"
         rf"[\w<>\[\],.?]+\s+({_ID})\s*\{{\s*(?:get|set|init)\b", "prop"),
    ],
    "swift": [
        (rf"^\s*(?:(?:public|private|fileprivate|internal|open|final|static|class|indirect|{_ANN})\s*)*"
         rf"(?:class|struct|enum|protocol|extension|actor)\s+({_ID})", "class"),
        (rf"^\s*(?:(?:public|private|fileprivate|internal|open|static|class|mutating|nonmutating|override|"
         rf"final|required|convenience|dynamic|{_ANN})\s*)*func\s+(?:<[^>]*>\s*)?({_ID})\s*[<(]", "fn"),
        (rf"^\s*(?:(?:public|private|fileprivate|internal|required|convenience|override)\s+)*(init|deinit)\??\s*\(", "fn"),
        (rf"^\s*(?:(?:public|private|fileprivate|internal|static|class|lazy|weak|unowned|override)\s+)+"
         rf"(?:let|var)\s+({_ID})", "prop"),
        (rf"^[ \t]{{0,4}}(?:let|var)\s+({_ID})", "prop"),
    ],
    "dart": [
        (rf"^\s*(?:(?:abstract|sealed|base|final|interface|mixin|augment)\s+)*"
         rf"(?:class|mixin|enum|extension|typedef)\s+({_ID})", "class"),
        (rf"^\s*(?:(?:static|abstract|external|factory|const)\s+)*"
         rf"(?!(?:if|for|while|switch|catch|return|assert|await|throw|new)\b)"
         rf"[\w<>?,\[\]]+\s+({_ID})\s*(?:<[^>]*>)?\s*\(", "fn"),
        (rf"^\s*(?:(?:static|abstract|external)\s+)*[\w<>?,\[\]]+\s+({_ID})\s*=\s*(?:async\s*)?\(", "fn"),
    ],
    "c": CPP_PATTERNS,
    "cpp": CPP_PATTERNS,
    "ruby": [(r"^\s*def\s+(?:self\.)?(\w+[?!=]?)", "fn"), (r"^\s*(?:class|module)\s+([\w:]+)", "class")],
    "php": [(r"^\s*(?:\w+\s+)*function\s+(\w+)", "fn"), (r"^\s*(?:\w+\s+)*(?:class|interface|trait)\s+(\w+)", "class")],
    "lua": [(r"^\s*(?:local\s+)?function\s+([\w.:]+)", "fn")],
    "gdscript": [
        (r"^\s*(?:static\s+)?func\s+(\w+)", "fn"),
        (r"^class_name\s+(\w+)", "class"),
        (r"^\s*class\s+(\w+)", "class"),
        (r"^signal\s+(\w+)", "signal"),
    ],
    "shell": [(r"^\s*(?:function\s+)?(\w[\w-]*)\s*\(\)\s*\{?", "fn")],
    "powershell": [(r"^\s*function\s+([\w-]+)", "fn")],
}


def _comment_head(text: str) -> str:
    """First meaningful comment or docstring line near the top of a file."""
    for line in text.splitlines()[:30]:
        s = line.strip()
        if not s or s.startswith(("#!", "import ", "from ", "package ", "use ", "'use", '"use')):
            continue
        for prefix in ("///", "//!", "//", "#", "--", "/**", "/*", "*", '"""', "'''", "<!--"):
            if s.startswith(prefix):
                s = s[len(prefix):].strip(" */-\"'>")
                if len(s) > 3:
                    return s[:200]
                break
        else:
            return ""
    return ""


def _python_outline(text: str) -> Optional[Outline]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    out = Outline("python")
    doc = ast.get_docstring(tree)
    if doc:
        out.head = doc.strip().splitlines()[0][:200]
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            out.symbols.append((node.name, "class", node.lineno))
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.symbols.append((f"{node.name}.{sub.name}", "method", sub.lineno))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.symbols.append((node.name, "fn", node.lineno))
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id.isupper():
                    out.symbols.append((t.id, "const", node.lineno))
    return out


def _match(lines: list[str], patterns: list[tuple[re.Pattern, str]], base_line: int = 0) -> list[tuple[str, str, int]]:
    """Line-by-line symbol scan. `base_line` offsets results (used for <script> blocks)."""
    found: list[tuple[str, str, int]] = []
    seen: set[tuple[str, int]] = set()
    for i, line in enumerate(lines, 1):
        for rx, kind in patterns:
            m = rx.match(line)
            if m and (m.group(1), i) not in seen:
                seen.add((m.group(1), i))
                found.append((m.group(1), kind, base_line + i))
                break
    return found


_SCRIPT_RX = re.compile(r"<script\b[^>]*>(.*?)</script>", re.S | re.I)


def _component_outline(lang: str, text: str) -> Outline:
    """Vue/Svelte: scan only the <script> blocks, keeping real file line numbers."""
    out = Outline(lang, head=_comment_head(text))
    pats = [(re.compile(p), k) for p, k in PATTERNS["js"]]
    for m in _SCRIPT_RX.finditer(text):
        base = text[: m.start(1)].count("\n")
        out.symbols.extend(_match(m.group(1).splitlines(), pats, base))
    return out


def _go_outline(text: str) -> Outline:
    """Go: record receiver methods as `Type.Method` so where_is finds them."""
    out = Outline("go", head=_comment_head(text))
    rx_method = re.compile(r"^func\s+\(([^)]*)\)\s+(\w+)")
    rx_func = re.compile(r"^func\s+(\w+)")
    rx_type = re.compile(r"^type\s+(\w+)")
    for i, line in enumerate(text.splitlines(), 1):
        m = rx_method.match(line)
        if m:
            recv = m.group(1).strip().split()
            rtype = (recv[-1] if recv else "").lstrip("*").split("[")[0]
            name = f"{rtype}.{m.group(2)}" if rtype else m.group(2)
            out.symbols.append((name, "method", i))
            continue
        m = rx_func.match(line)
        if m:
            out.symbols.append((m.group(1), "fn", i))
            continue
        m = rx_type.match(line)
        if m:
            out.symbols.append((m.group(1), "type", i))
    return out


def outline(relpath: str, text: str) -> Outline:
    lang = LANG_BY_EXT.get(Path(relpath).suffix.lower(), "text")
    result: Optional[Outline] = None
    if lang == "python":
        result = _python_outline(text)
    elif lang in ("vue", "svelte"):
        result = _component_outline(lang, text)
    elif lang == "go":
        result = _go_outline(text)
    if result is None:
        result = Outline(lang, head=_comment_head(text))
        if lang == "markdown":
            for i, line in enumerate(text.splitlines(), 1):
                m = re.match(r"^(#{1,3})\s+(.+)", line)
                if m:
                    result.symbols.append((m.group(2).strip()[:80], "h" + str(len(m.group(1))), i))
            if not result.head and result.symbols:
                result.head = result.symbols[0][0]
        else:
            pats = [(re.compile(p), k) for p, k in PATTERNS.get(lang, [])]
            if pats:
                result.symbols.extend(_match(text.splitlines(), pats))
    result.lines = text.count("\n") + 1
    return result
