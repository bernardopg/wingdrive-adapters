"""Review checks that read or run an adapter's code: code scan, its own tests and the contract harness.

The code scan is static and errs on the side of asking: anything it flags must either be declared
in `[adapter.permissions]` or removed. It cannot prove an adapter safe; the sandbox the daemon
runs adapters in (bubblewrap on Linux) enforces the declared paths and network, and a maintainer
still reads the code. The tests and the harness run under bubblewrap without network when it is
available, so a submission cannot reach the internet from the registry's CI either.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}
# Modules that open network connections. `urllib` alone also parses URLs, so only its `request`
# submodule counts.
NETWORK_MODULES = {
    "urllib.request", "http.client", "socket", "ssl", "ftplib", "smtplib", "imaplib", "poplib",
    "telnetlib", "xmlrpc.client", "requests", "httpx", "aiohttp", "urllib3", "websocket", "websockets",
}
DYNAMIC_CODE = {
    "eval", "exec", "compile", "__import__", "importlib.import_module", "importlib.__import__",
    "pickle.load", "pickle.loads", "marshal.load", "marshal.loads", "runpy.run_path", "runpy.run_module",
}
SHELL_OUT = re.compile(
    r"^(subprocess\.(run|Popen|call|check_call|check_output|getoutput|getstatusoutput)"
    r"|os\.(system|popen|exec\w*|spawn\w*|posix_spawn\w*)|pty\.spawn|asyncio\.create_subprocess_\w+)$"
)
URL = re.compile(r"\b(?:https?|wss?)://([^/\s:?#\"'<>]+)", re.IGNORECASE)
SECRET_NAME = re.compile(r"(token|password|passwd|secret|api_?key|private_?key)", re.IGNORECASE)
SECRET_PATTERNS = {
    "GitHub token": re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "OpenAI or Anthropic key": re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{32,}"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----\s*[A-Za-z0-9+/=\s]{100,}"),
}
# Tests of redaction code need values shaped like credentials. They pass the scan when they are
# plainly fake: few distinct characters, a run of the alphabet or digits, or a word saying so.
FAKE_MARKERS = re.compile(r"abcdefgh|01234567|ABCDEFGH|example|fake|dummy|placeholder", re.IGNORECASE)


def _looks_fake(value: str) -> bool:
    tail = re.sub(r"[^A-Za-z0-9]", "", value)[-24:]
    return len(set(tail)) <= 4 or bool(FAKE_MARKERS.search(value))
SKIPPED_DIRS = {"tests", "fixtures", "__pycache__", ".git"}
TEST_TIMEOUT = 300
HARNESS_TIMEOUT = 600


@dataclass
class CheckResult:
    """One row of the review table."""

    name: str
    passed: bool
    detail: str = ""


# ── Code scan ───────────────────────────────────────────────────────────────


def _code_files(directory: Path, kit_digests: set[str]) -> list[Path]:
    """The adapter's own Python files: not its tests or fixtures, not a released kit copy."""
    files = []
    for path in sorted(directory.rglob("*.py")):
        relative = path.relative_to(directory)
        if any(part in SKIPPED_DIRS for part in relative.parts[:-1]):
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() in kit_digests:
            continue
        files.append(path)
    return files


def _docstrings(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _aliases(tree: ast.AST) -> dict[str, str]:
    """Local name -> qualified name, from the file's imports."""
    names = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return names


def _qualified(func: ast.AST, aliases: dict[str, str]) -> str:
    parts = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if not isinstance(func, ast.Name):
        return ""
    parts.append(aliases.get(func.id, func.id))
    return ".".join(reversed(parts))


def _imported_modules(tree: ast.AST) -> list[tuple[int, str]]:
    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules += [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            modules.append((node.lineno, node.module))
            modules += [(node.lineno, f"{node.module}.{alias.name}") for alias in node.names]
    return modules


def _host_declared(host: str, declared: list[str]) -> bool:
    host = host.lower()
    if host in LOOPBACK:
        return True
    for entry in declared:
        if entry.startswith("*.") and (host.endswith(entry[1:]) or host == entry[2:]):
            return True
        if host == entry:
            return True
    return False


def _path_declared(literal: str, declared: list[str]) -> bool:
    literal = literal.replace("\\", "/").rstrip("/")
    for entry in declared:
        entry = entry.replace("\\", "/").rstrip("/")
        if not entry.startswith("~"):
            continue
        if literal == entry or literal.startswith(entry + "/") or entry.startswith(literal + "/"):
            return True
    return False


def scan_code(directory: Path, manifest: dict, kit_digests: set[str]) -> list[str]:
    """Problems with what the adapter's code does compared with what its manifest declares."""
    meta = manifest.get("adapter", {})
    permissions = meta.get("permissions", {})
    hosts = permissions.get("network", [])
    reads = permissions.get("read", [])
    tools = permissions.get("tools", [])
    local_modules = {p.stem for p in directory.glob("*.py")} | {p.name for p in directory.iterdir() if p.is_dir()}
    problems = []
    uses_network = []

    for path in _code_files(directory, kit_digests):
        relative = path.relative_to(directory).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except (SyntaxError, UnicodeDecodeError) as error:
            problems.append(f"{relative}: cannot be parsed: {error}")
            continue
        aliases = _aliases(tree)
        docstrings = _docstrings(tree)

        for line, module in _imported_modules(tree):
            top = module.split(".")[0]
            if module in NETWORK_MODULES:
                uses_network.append(f"{relative}:{line} imports {module}")
            if top not in sys.stdlib_module_names and top not in local_modules and top != "__future__":
                problems.append(
                    f"{relative}:{line} imports {top}, which is not in the Python standard library; "
                    "the daemon runs adapters with `python3 -I` and no installed packages"
                )

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _qualified(node.func, aliases)
                if name in DYNAMIC_CODE:
                    problems.append(f"{relative}:{node.lineno} runs code built at runtime ({name})")
                elif SHELL_OUT.match(name):
                    program = _program(node)
                    if not tools:
                        problems.append(
                            f"{relative}:{node.lineno} starts a program ({name}) but permissions.tools declares none"
                        )
                    elif program is not None and Path(program).name not in tools:
                        problems.append(
                            f"{relative}:{node.lineno} starts {program}, which permissions.tools does not declare"
                        )
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                for host in URL.findall(node.value):
                    if not _host_declared(host, hosts):
                        problems.append(
                            f"{relative}:{node.lineno} contacts {host}, which permissions.network does not declare"
                        )
                text = node.value.strip()
                if re.match(r"^~[/\\]\S", text) and not _path_declared(text, reads):
                    problems.append(f"{relative}:{node.lineno} reads {text}, which permissions.read does not declare")

    if uses_network and not hosts:
        problems.append(f"{uses_network[0]} but permissions.network declares no host")

    secret_fields = {f.get("key") for f in meta.get("config", []) if f.get("secret")}
    declared_secrets = set(permissions.get("secrets", []))
    for key in sorted(secret_fields - declared_secrets):
        problems.append(f"config field {key} is secret but permissions.secrets does not list it")
    for key in sorted(declared_secrets - secret_fields):
        problems.append(f"permissions.secrets lists {key}, which is not a secret config field")
    for field in meta.get("config", []):
        key = field.get("key", "")
        if SECRET_NAME.search(key) and not field.get("secret") and field.get("type", "string") == "string":
            problems.append(f"config field {key} looks like a secret: mark it `secret = true`")
    return problems


def _program(call: ast.Call) -> str | None:
    if not call.args:
        return None
    first = call.args[0]
    if isinstance(first, (ast.List, ast.Tuple)) and first.elts and isinstance(first.elts[0], ast.Constant):
        value = first.elts[0].value
        return value if isinstance(value, str) else None
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value.split()[0] if first.value.split() else None
    return None


def scan_secrets(directory: Path) -> list[str]:
    """Credentials committed anywhere in the adapter folder, fixtures included."""
    problems = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or ".git" in path.parts or path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        relative = path.relative_to(directory).as_posix()
        for label, pattern in SECRET_PATTERNS.items():
            match = next((m for m in pattern.finditer(text) if not _looks_fake(m.group(0))), None)
            if match:
                line = text.count("\n", 0, match.start()) + 1
                problems.append(f"{relative}:{line} holds what looks like a {label}; remove it and revoke it")
    return problems


# ── Running code ────────────────────────────────────────────────────────────


def offline_prefix() -> list[str]:
    """A command prefix that cuts the network off, or [] where bubblewrap cannot run."""
    if os.environ.get("WINGREG_SANDBOX") == "off" or not sys.platform.startswith("linux"):
        return []
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return []
    prefix = [bwrap, "--dev-bind", "/", "/", "--unshare-net", "--die-with-parent", "--"]
    probe = subprocess.run([*prefix, "true"], capture_output=True)
    return prefix if probe.returncode == 0 else []


def _tail(text: str, lines: int = 40) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def run_tests(directory: Path, prefix: list[str], python: str = sys.executable) -> CheckResult:
    """The adapter's own `tests/`, with the standard library's unittest and no network."""
    tests = directory / "tests"
    if not tests.is_dir():
        return CheckResult("Adapter tests", False, "tests/ is missing")
    env = {key: value for key, value in os.environ.items() if not key.startswith(("GITHUB_TOKEN", "GH_TOKEN", "ACTIONS_"))}
    command = [*prefix, python, "-m", "unittest", "discover", "-s", str(tests), "-t", str(tests)]
    try:
        result = subprocess.run(command, cwd=directory, capture_output=True, text=True, timeout=TEST_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        return CheckResult("Adapter tests", False, f"did not finish in {TEST_TIMEOUT} s")
    summary = _tail(result.stderr, 3)
    if result.returncode != 0:
        return CheckResult("Adapter tests", False, _tail(result.stdout + "\n" + result.stderr))
    if "Ran 0 tests" in result.stderr:
        return CheckResult("Adapter tests", False, "tests/ holds no test")
    return CheckResult("Adapter tests", True, summary)


def find_harness() -> list[str] | None:
    """The contract harness on this machine: `wing-adapter-test`, or `wing adapter test`."""
    override = os.environ.get("WING_ADAPTER_TEST")
    if override:
        return [override]
    found = shutil.which("wing-adapter-test")
    if found:
        return [found]
    wing = shutil.which("wing")
    if wing:
        probe = subprocess.run([wing, "adapter", "test", "--help"], capture_output=True)
        if probe.returncode == 0:
            return [wing, "adapter", "test"]
    return None


def run_harness(directory: Path, harness: list[str]) -> CheckResult:
    """`wing adapter test` on the adapter's fixtures, as the daemon runs it (sandbox included)."""
    cases = [p for p in (directory / "fixtures").glob("*/expected.json")] if (directory / "fixtures").is_dir() else []
    if not cases:
        return CheckResult("Contract harness", False, "fixtures/ has no case: add fixtures/<case>/expected.json")
    env = {key: value for key, value in os.environ.items() if not key.startswith(("GITHUB_TOKEN", "GH_TOKEN", "ACTIONS_"))}
    try:
        result = subprocess.run(
            [*harness, str(directory)], capture_output=True, text=True, timeout=HARNESS_TIMEOUT, env=env
        )
    except subprocess.TimeoutExpired:
        return CheckResult("Contract harness", False, f"did not finish in {HARNESS_TIMEOUT} s")
    output = (result.stdout + "\n" + result.stderr).strip()
    if result.returncode != 0:
        return CheckResult("Contract harness", False, _tail(output))
    return CheckResult("Contract harness", True, output.splitlines()[-1] if output else "")


def kit_digests(releases: Path) -> set[str]:
    return set(json.loads(releases.read_text()).values()) if releases.is_file() else set()
