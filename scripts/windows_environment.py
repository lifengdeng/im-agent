#!/usr/bin/env python3
"""Prepare an isolated Windows Codex installation; never deploy the agent."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from im_agent.processes import background_options
TOOLCHAIN = json.loads((ROOT / "config/windows-toolchain.json").read_text(encoding="utf-8"))
LOCK = TOOLCHAIN["codex"]
CONFIG = 'cli_auth_credentials_store = "file"\napproval_policy = "on-request"\nsandbox_mode = "workspace-write"\n'
MARKER = {"schema": 1, "profile": "isolated-codex"}
REQUIRED = ("codex-package.json", "bin/codex.exe", "bin/codex-code-mode-host.exe", "codex-path/rg.exe", "codex-resources/codex-command-runner.exe", "codex-resources/codex-windows-sandbox-setup.exe")
AUTH_ENV = ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_AUTH_TOKEN", "OPENAI_BASE_URL", "CHATGPT_BASE_URL", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID")


class PreparationError(RuntimeError):
    pass


def reject_links(path: Path) -> None:
    for part in (path, *path.parents):
        if not part.exists() and not part.is_symlink():
            continue
        info = part.lstat()
        if part.is_symlink() or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise PreparationError("reparse_or_symlink_path_rejected")


def runtime_path(value: str) -> Path:
    path = Path(value).absolute()
    reject_links(path)
    path = path.resolve()
    if path == Path(path.anchor) or path == Path.home().resolve():
        raise PreparationError("runtime_root_too_broad")
    if path.is_relative_to(ROOT) or ROOT.is_relative_to(path):
        raise PreparationError("runtime_must_be_outside_source")
    personal = Path.home() / ".codex"
    if path.is_relative_to(personal) or personal.is_relative_to(path):
        raise PreparationError("personal_codex_home_rejected")
    return path


def write_new_or_same(path: Path, data: str) -> None:
    reject_links(path)
    if path.exists():
        if path.read_text(encoding="utf-8") != data:
            raise PreparationError("existing_file_preserved:" + path.name)
        return
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(data)


def command(args: list[str], *, env=None, cwd=None, timeout=30) -> subprocess.CompletedProcess:
    return subprocess.run(args, env=env, cwd=cwd, capture_output=True, timeout=timeout, encoding="utf-8", errors="replace", **background_options())


def private_acl(path: Path) -> None:
    sid = command(["powershell.exe", "-NoProfile", "-Command", "([Security.Principal.WindowsIdentity]::GetCurrent()).User.Value"])
    value = sid.stdout.strip()
    if sid.returncode or not value.startswith("S-1-") or not all(c.isdigit() or c in "S-" for c in value):
        raise PreparationError("windows_identity_unavailable")
    result = command(["icacls.exe", str(path), "/inheritance:r", "/grant:r", "*" + value + ":(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"])
    if result.returncode:
        raise PreparationError("private_acl_failed")


def initialize(root: Path) -> None:
    marker = root / "instance.json"
    if root.exists() and any(root.iterdir()):
        reject_links(marker)
        if not marker.is_file() or json.loads(marker.read_text(encoding="utf-8")) != MARKER:
            raise PreparationError("unowned_runtime_root_preserved")
    root.mkdir(parents=True, exist_ok=True)
    private_acl(root)
    write_new_or_same(marker, json.dumps(MARKER, sort_keys=True) + "\n")
    for name in ("codex-home", "tools", "downloads", "workspace", "bin"):
        path = root / name
        reject_links(path)
        path.mkdir(exist_ok=True)
    config = root / "codex-home/config.toml"
    if config.exists():
        reject_links(config)
        if tomllib.loads(config.read_text(encoding="utf-8")).get("cli_auth_credentials_store") != "file":
            raise PreparationError("existing_credential_storage_must_be_file")
    else:
        write_new_or_same(config, CONFIG)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download_range(url: str, start: int, end: int, total: int, target: Path) -> None:
    # A public range key also avoids stale intermediary responses for another span.
    query = ("&" if "?" in url else "?") + f"im_agent_range={start}-{end}"
    request = urllib.request.Request(url + query, headers={"Range": f"bytes={start}-{end}", "Cache-Control": "no-cache"})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=60) as response, target.open("wb") as stream:
                if response.status != 206 or response.headers.get("Content-Range") != f"bytes {start}-{end}/{total}":
                    raise PreparationError("download_range_response_invalid")
                size = 0
                while chunk := response.read(64 * 1024):
                    size += len(chunk)
                    if size > end - start + 1:
                        raise PreparationError("download_range_size_exceeded")
                    stream.write(chunk)
            if size != end - start + 1:
                raise PreparationError("download_range_incomplete")
            return
        except (OSError, PreparationError):
            if attempt:
                raise


def download_parts(url: str, size: int, target: Path) -> None:
    # Bounded parallel HTTP ranges avoid a single slow large-file connection.
    # Each range is validated; the caller still checks the whole signed-off SHA.
    with tempfile.TemporaryDirectory(prefix="download-parts-", dir=target.parent) as temporary:
        directory = Path(temporary)
        count = min(8, size)
        width = min((size + count - 1) // count, 2 * 1024 * 1024)
        spans = [(start, min(start + width - 1, size - 1)) for start in range(0, size, width)]
        files = [directory / str(i) for i in range(len(spans))]
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(download_range, url, start, end, size, file) for (start, end), file in zip(spans, files)]
            for future in futures:
                future.result()
        with target.open("wb") as destination:
            for file in files:
                with file.open("rb") as source:
                    shutil.copyfileobj(source, destination)


def download(root: Path, artifact=None) -> Path:
    artifact = LOCK if artifact is None else artifact
    archive = root / "downloads" / artifact["archive"]
    reject_links(archive)
    if archive.exists():
        if archive.stat().st_size == artifact["size"] and digest(archive) == artifact["sha256"]:
            return archive
        raise PreparationError("existing_archive_checksum_mismatch")
    errors = []
    for url in artifact["sources"] * 2:
        fd, temporary = tempfile.mkstemp(prefix="codex-download-", suffix=".part", dir=archive.parent)
        os.close(fd)
        try:
            download_parts(url, artifact["size"], Path(temporary))
            part = Path(temporary)
            if part.stat().st_size != artifact["size"] or digest(part) != artifact["sha256"]:
                raise PreparationError("download_checksum_mismatch")
            part.rename(archive)
            return archive
        except (OSError, PreparationError) as exc:
            errors.append(str(exc) if isinstance(exc, PreparationError) else type(exc).__name__ + (":" + str(exc.code) if hasattr(exc, "code") else ""))
            Path(temporary).unlink(missing_ok=True)
    raise PreparationError("official_package_download_failed:" + ",".join(errors))


def extract(archive: Path, target: Path) -> None:
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or any(c in member.name for c in '\\:*?"<>|') or not (member.isdir() or member.isfile()):
                raise PreparationError("unsafe_archive_member")
            if not (target / member.name).resolve().is_relative_to(target.resolve()):
                raise PreparationError("unsafe_archive_path")
        for member in members:
            path = target / member.name
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as source, path.open("xb") as destination:
                    shutil.copyfileobj(source, destination)


def package(root: Path) -> Path:
    return root / "tools" / ("codex-" + LOCK["version"])


def verify_package(path: Path) -> bool:
    receipt = path / ".im-agent-package.json"
    reject_links(receipt)
    if not receipt.is_file():
        return False
    data = json.loads(receipt.read_text(encoding="utf-8"))
    approved = {LOCK["sha256"], LOCK["npm_distribution"]["sha256"]}
    if data.get("archive_sha256") not in approved or data.get("version") != LOCK["version"] or not all(name in data.get("files", {}) for name in REQUIRED):
        return False
    for name, expected in data["files"].items():
        candidate = path / name
        if not candidate.resolve().is_relative_to(path.resolve()):
            return False
        reject_links(candidate)
        if not candidate.is_file() or digest(candidate) != expected:
            return False
    actual = {p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file() and p != receipt}
    return actual == set(data["files"])


def codex_env(root: Path, inherited=None) -> dict:
    env = dict(os.environ if inherited is None else inherited)
    for name in AUTH_ENV:
        env.pop(name, None)
    env["CODEX_HOME"] = str(root / "codex-home")
    return env


def validate_home(root: Path) -> None:
    for name in ("tools", "downloads", "workspace", "bin", "codex-home"):
        reject_links(root / name)
    reject_links(root / "codex-home/config.toml")
    if json.loads((root / "instance.json").read_text(encoding="utf-8")) != MARKER:
        raise PreparationError("runtime_identity_mismatch")
    if tomllib.loads((root / "codex-home/config.toml").read_text(encoding="utf-8")).get("cli_auth_credentials_store") != "file":
        raise PreparationError("credential_storage_must_be_file")


def checked_source():
    from project_checks import Repository, check_repository
    repo = Repository(ROOT)
    if repo.run("status", "--porcelain=v1", "--untracked-files=all").stdout.strip() or not check_repository(repo, "head")["passed"]:
        raise PreparationError("clean_checked_git_commit_required")
    return repo


def signed_installer(root: Path, key: str, publisher: str) -> tuple[Path, dict]:
    checked_source()
    initialize(root)
    artifact = TOOLCHAIN[key]
    installer = download(root, artifact)
    quoted = str(installer).replace("'", "''")
    script = "$s=Get-AuthenticodeSignature -LiteralPath '" + quoted + "'; @{status=$s.Status.ToString();subject=$s.SignerCertificate.Subject}|ConvertTo-Json -Compress"
    signature = command(["powershell.exe", "-NoProfile", "-Command", script])
    data = json.loads(signature.stdout) if signature.returncode == 0 else {}
    if data.get("status") != "Valid" or publisher not in data.get("subject", ""):
        raise PreparationError("installer_signature_invalid")
    return installer, artifact


def prepare_wsl(root: Path) -> dict:
    installer, artifact = signed_installer(root, "wsl", "O=Microsoft Corporation")
    log = root / "downloads/wsl-install.log"
    reject_links(log)
    result = command(["msiexec.exe", "/i", str(installer), "/qn", "/norestart", "/L*v", str(log)], timeout=300)
    if result.returncode not in (0, 3010):
        raise PreparationError("wsl_msi_failed:" + str(result.returncode))
    return {"wsl_package": artifact["version"], "sha256_verified": True, "signature": "Microsoft valid", "installer_exit_code": result.returncode, "restart_required": result.returncode == 3010, "reboot_performed": False}


def prepare_docker(root: Path) -> dict:
    # This entry repairs a missing/broken installation, never upgrades a live engine.
    for base in (Path(os.environ["LOCALAPPDATA"]) / "Programs/DockerDesktop", Path(os.environ["ProgramFiles"]) / "Docker/Docker"):
        docker = base / "resources/bin/docker.exe"
        if docker.is_file():
            probe = command([str(docker), "version", "--format", "{{json .Server}}"], timeout=20)
            if probe.returncode == 0:
                raise PreparationError("responding_docker_engine_preserved")
            if not any(text in probe.stderr.lower() for text in ("cannot find the file", "system cannot find", "no such file")):
                raise PreparationError("docker_state_uncertain_preserved")
    backend = command(["powershell.exe", "-NoProfile", "-Command", "@(Get-Process -Name 'com.docker.backend' -ErrorAction SilentlyContinue).Count"])
    if backend.returncode or backend.stdout.strip() != "0":
        raise PreparationError("existing_docker_backend_preserved")
    installer, artifact = signed_installer(root, "docker_desktop", "Docker Inc")
    result = command([str(installer), "install", "--user", "--quiet", "--backend=wsl-2"], timeout=600)
    if result.returncode not in (0, 3010):
        raise PreparationError("docker_installer_failed:" + str(result.returncode))
    return {"docker_desktop_package": artifact["version"], "sha256_verified": True, "signature": "Docker valid", "installer_exit_code": result.returncode, "restart_required": result.returncode == 3010, "engine_ready": "not_yet_checked", "agent_deployed": False}


def package_tree(stage: Path) -> Path:
    vendor = stage / "package/vendor" / LOCK["target"]
    if not vendor.is_dir():
        return stage
    metadata = vendor / "npm-distribution"
    metadata.mkdir()
    for name in ("package.json", "README.md", "LICENSE"):
        source = stage / "package" / name
        if source.is_file():
            shutil.copy2(source, metadata / name)
    return vendor


def prepare(root: Path) -> dict:
    repo = checked_source()
    initialize(root)
    destination = package(root)
    if destination.exists():
        if not verify_package(destination):
            raise PreparationError("existing_package_preserved_but_invalid")
    else:
        artifact = LOCK["npm_distribution"]
        archive = download(root, artifact)
        stage = Path(tempfile.mkdtemp(prefix="codex-stage-", dir=root / "tools"))
        try:
            extract(archive, stage)
            candidate = package_tree(stage)
            if not all((candidate / name).is_file() for name in REQUIRED):
                raise PreparationError("incomplete_official_package")
            files = {p.relative_to(candidate).as_posix(): digest(p) for p in candidate.rglob("*") if p.is_file()}
            receipt = {"archive_sha256": artifact["sha256"], "version": LOCK["version"], "files": files}
            (candidate / ".im-agent-package.json").write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
            candidate.rename(destination)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    # Private generated convenience entry. No global PATH or environment changes.
    values = [str(Path(sys.executable).resolve()), str(ROOT / "scripts/windows_environment.py"), str(root)]
    if any(any(c in value for c in '\r\n"%') for value in values):
        raise PreparationError("launcher_path_characters_unsupported")
    launcher = '@echo off\r\n"' + values[0] + '" -B "' + values[1] + '" codex --runtime-root "' + values[2] + '" -- %*\r\n'
    write_new_or_same(root / "bin/codex-agent.cmd", launcher.replace("\r\n", "\n"))
    result = status(root)
    if result["codex_version"] != "codex-cli " + LOCK["version"]:
        raise PreparationError("installed_version_mismatch")
    result["prepared_from_commit"] = repo.run("rev-parse", "HEAD").stdout.decode().strip()
    return result


def status(root: Path) -> dict:
    validate_home(root)
    installed = verify_package(package(root))
    result = {"runtime_root": str(root), "package_verified": installed, "codex_version": None, "credential_storage": "file", "codex_home": str(root / "codex-home"), "login": "not_checked", "agent_deployed": False}
    if installed:
        binary = str(package(root) / "bin/codex.exe")
        env = codex_env(root)
        version = command([binary, "--version"], env=env, cwd=root / "workspace")
        result["codex_version"] = version.stdout.strip() if version.returncode == 0 else "failed"
        login = command([binary, "login", "status"], env=env, cwd=root / "workspace")
        # Never emit authentication output or read auth.json.
        result["login"] = "authenticated" if login.returncode == 0 else "not_authenticated" if "not logged in" in (login.stdout + login.stderr).lower() else "check_failed"
    return result


def docker_status() -> dict:
    binary = Path(os.environ["LOCALAPPDATA"]) / "Programs/DockerDesktop/resources/bin/docker.exe"
    if not binary.is_file():
        binary = Path(os.environ["ProgramFiles"]) / "Docker/Docker/resources/bin/docker.exe"
    result = {"engine_ready": False, "server_version": None, "os": None, "architecture": None}
    if not binary.is_file():
        return result
    # Explicit local Linux pipe; never follow inherited remote Docker endpoints.
    env = dict(os.environ)
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        env.pop(name, None)
    probe = command([str(binary), "--host", "npipe:////./pipe/dockerDesktopLinuxEngine", "version", "--format", "{{json .Server}}"], env=env, timeout=15)
    if probe.returncode == 0:
        data = json.loads(probe.stdout)
        result.update(engine_ready=data.get("Os") == "linux", server_version=data.get("Version"), os=data.get("Os"), architecture=data.get("Arch"))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare-codex", "prepare-wsl", "prepare-docker", "status", "codex"))
    parser.add_argument("--runtime-root", required=True)
    args, remaining = parser.parse_known_args()
    if os.name != "nt" or platform.machine().lower() not in ("amd64", "x86_64"):
        parser.error("Windows x64 is required")
    try:
        root = runtime_path(args.runtime_root)
        if args.command == "codex":
            validate_home(root)
            if not verify_package(package(root)):
                raise PreparationError("package_verification_failed")
            cli_args = remaining[1:] if remaining[:1] == ["--"] else remaining
            return subprocess.call([str(package(root) / "bin/codex.exe"), *cli_args], env=codex_env(root), cwd=root / "workspace")
        if remaining:
            parser.error("unexpected arguments")
        actions = {"prepare-codex": prepare, "prepare-wsl": prepare_wsl, "prepare-docker": prepare_docker, "status": status}
        result = actions[args.command](root)
        if args.command == "status":
            result["docker"] = docker_status()
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (OSError, ValueError, PreparationError, subprocess.TimeoutExpired, tarfile.TarError) as exc:
        print("im-agent environment: " + (str(exc) if isinstance(exc, PreparationError) else type(exc).__name__), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
