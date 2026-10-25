"""Small, offline repository checks; never print inspected file contents."""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from im_agent.processes import background_options

MAX_FILE_BYTES = 1024 * 1024
_PRIVATE_KEY = re.compile(rb"-----BEGIN (?:OPENSSH |RSA |EC |DSA |ENCRYPTED )?PRIVATE KEY-----")
_TOKEN = re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|sk-[A-Za-z0-9_-]{24,})\b")
_ASSIGNMENT = re.compile(
    r'''(?im)^\s*(?:FEISHU_APP_SECRET|MODEL_API_KEY|API_KEY|ACCESS_TOKEN|GITHUB_TOKEN|PASSWORD)\s*[=:]\s*["']?([^\s"'#]+)'''
)
_LOCAL_ROOTS = {".local", ".work", "outputs", "runtime", "state", "sessions", "snapshots", "backups", "logs"}
_GENERATED_PARTS = {"secrets", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache"}
_PRIVATE_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".log", ".pyc", ".pyo", ".pem", ".key", ".p12", ".pfx")


class CheckError(RuntimeError):
    pass


@dataclass(frozen=True)
class Issue:
    path: str
    code: str


def find_git() -> str:
    configured = os.environ.get("IM_AGENT_GIT_EXECUTABLE", "").strip()
    if configured:
        if not Path(configured).is_file():
            raise CheckError("configured_git_executable_missing")
        return configured
    if os.name == "nt":
        for variable in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(variable)
            if base:
                candidate = Path(base) / "Git" / "cmd" / "git.exe"
                if candidate.is_file():
                    return str(candidate)
        candidate = shutil.which("git.exe")
        if candidate and "cygwin" not in candidate.lower():
            return candidate
        raise CheckError("native_git_for_windows_required")
    candidate = shutil.which("git")
    if not candidate:
        raise CheckError("git_executable_missing")
    return candidate


class Repository:
    def __init__(self, root: Path, git: str | None = None):
        self.root = root.resolve()
        self.git = git or find_git()
        resolved = self.run("rev-parse", "--show-toplevel").stdout.decode("utf-8", "surrogateescape").strip()
        if Path(resolved).resolve() != self.root:
            raise CheckError("repository_root_mismatch")

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run([self.git, "-C", str(self.root), *args], capture_output=True, timeout=20, **background_options())
        if check and result.returncode:
            raise CheckError("git_operation_failed:" + args[0])
        return result

    def value(self, key: str) -> str | None:
        result = self.run("config", "--local", "--get", key, check=False)
        if result.returncode == 1:
            return None
        if result.returncode:
            raise CheckError("git_config_read_failed")
        return result.stdout.decode("utf-8", "surrogateescape").strip()

    def hidden_paths(self) -> list[str]:
        result = []
        for entry in self.run("ls-files", "-v", "-z").stdout.split(b"\0"):
            if not entry:
                continue
            flag = chr(entry[0])
            if flag.islower() or flag == "S":
                result.append(entry[2:].decode("utf-8", "surrogateescape"))
        return result

    def entries(self, mode: str) -> list[tuple[str, str, bytes]]:
        if mode == "worktree":
            names = self.run("ls-files", "--cached", "--others", "--exclude-standard", "-z").stdout.split(b"\0")
            result = []
            for raw in sorted(set(filter(None, names))):
                name = raw.decode("utf-8", "surrogateescape")
                file = self.root / name
                if file.is_symlink():
                    result.append((name, "120000", b""))
                elif file.is_file():
                    if not file.resolve().is_relative_to(self.root):
                        result.append((name, "120000", b""))
                    else:
                        with file.open("rb") as stream:
                            result.append((name, "100644", stream.read(MAX_FILE_BYTES + 1)))
            return result
        if mode == "head":
            raw_entries = self.run("ls-tree", "-r", "-z", "HEAD").stdout
            result = []
            for item in filter(None, raw_entries.split(b"\0")):
                metadata, raw_name = item.split(b"\t", 1)
                file_mode, kind, oid = metadata.decode("ascii").split()
                name = raw_name.decode("utf-8", "surrogateescape")
                if kind != "blob":
                    result.append((name, "unsupported", b""))
                else:
                    result.append((name, file_mode, self.run("cat-file", "blob", oid).stdout))
            return result
        if mode != "staged":
            raise CheckError("unsupported_check_mode")
        changed = set(filter(None, self.run("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").stdout.split(b"\0")))
        result = []
        for item in filter(None, self.run("ls-files", "--stage", "-z").stdout.split(b"\0")):
            metadata, raw_name = item.split(b"\t", 1)
            if raw_name not in changed:
                continue
            file_mode, oid, stage = metadata.decode("ascii").split()
            if stage != "0":
                continue
            result.append((raw_name.decode("utf-8", "surrogateescape"), file_mode, self.run("cat-file", "blob", oid).stdout))
        return result


def blocked_path(name: str) -> bool:
    path = PurePosixPath(name)
    parts = tuple(part.lower() for part in path.parts)
    if not parts or path.is_absolute() or ".." in parts:
        return True
    if parts[0] in _LOCAL_ROOTS or any(part in _GENERATED_PARTS for part in parts):
        return True
    leaf = path.name.lower()
    if leaf in {"auth.json", "id_rsa", "id_ed25519"}:
        return True
    if (leaf == ".env" or leaf.startswith(".env.")) and leaf != ".env.example":
        return True
    if leaf.endswith(_PRIVATE_SUFFIXES) or any(marker in leaf for marker in (".db-wal", ".db-shm", ".sqlite-wal", ".sqlite-shm", ".sqlite3-wal", ".sqlite3-shm")):
        return True
    return False


def _placeholder(value: str) -> bool:
    lower = value.lower()
    return lower.startswith(("replace_", "your_", "example", "dummy", "<", "${", "$env:"))


def inspect_content(name: str, file_mode: str, data: bytes) -> list[Issue]:
    issues = []
    if blocked_path(name):
        issues.append(Issue(name, "private_or_generated_path"))
    if file_mode in {"120000", "160000", "unsupported"}:
        return [*issues, Issue(name, "symlink_or_embedded_repository_not_supported")]
    if len(data) > MAX_FILE_BYTES:
        return [*issues, Issue(name, "file_size_limit_exceeded")]
    if _PRIVATE_KEY.search(data) or _TOKEN.search(data):
        issues.append(Issue(name, "credential_pattern_detected"))
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        if name.endswith(".py"):
            issues.append(Issue(name, "python_source_not_utf8"))
        return issues
    if any(len(match.group(1)) >= 12 and not _placeholder(match.group(1)) for match in _ASSIGNMENT.finditer(text)):
        issues.append(Issue(name, "credential_assignment_detected"))
    if name.endswith(".py"):
        try:
            compile(text, name, "exec")
        except (SyntaxError, ValueError):
            issues.append(Issue(name, "python_syntax_invalid"))
    return issues


def check_repository(repo: Repository, mode: str = "worktree") -> dict:
    issues = [Issue(name, "hidden_tracked_file") for name in repo.hidden_paths()]
    sparse = repo.run("config", "--bool", "core.sparseCheckout", check=False)
    if sparse.returncode == 0 and sparse.stdout.strip() == b"true":
        issues.append(Issue(".", "sparse_checkout_not_supported"))
    if repo.run("ls-files", "--unmerged", "-z").stdout:
        issues.append(Issue(".", "unresolved_index_conflicts"))
    entries = repo.entries(mode)
    for name, file_mode, data in entries:
        issues.extend(inspect_content(name, file_mode, data))
    whitespace_args = {"head": ("show", "--format=", "--check", "HEAD"), "staged": ("diff", "--cached", "--check"), "worktree": ("diff", "--check")}
    if repo.run(*whitespace_args[mode], check=False).returncode:
        issues.append(Issue(".", "git_whitespace_check_failed"))
    return {"passed": not issues, "mode": mode, "checked_files": len(entries), "issues": [{"path": issue.path, "code": issue.code} for issue in issues]}
