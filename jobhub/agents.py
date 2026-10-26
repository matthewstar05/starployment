"""Connections to the AI agent that powers Luna, Starployment's drafting assistant.

A candidate can connect whichever account they already pay for:

* ``antigravity_cli`` - Google Antigravity via the local CLI binary (``agy``)
* ``claude_cli``  - a Claude Pro or Max plan, through the Claude Code app (``claude``)
* ``codex_cli``   - a ChatGPT Plus or Pro plan, through the Codex app (``codex``)
* ``anthropic_api`` / ``openai_api`` - an API key saved in the system credential vault

Every call is one prompt in, one text reply out. CLI agents run with their
tools, plugins, MCP servers, and web access disabled, inside an empty scratch
directory, so job-posting text cannot make them read or change local files.
Luna's output is always a draft that the candidate reviews before use.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from . import config

KEY_SERVICE = "Starployment agent key"


@dataclass(frozen=True)
class Backend:
    key: str
    name: str
    plan: str
    cli: str = ""
    install_url: str = ""
    install_command: str = ""
    login_args: tuple[str, ...] = ()
    default_model: str = ""
    models: tuple[tuple[str, str], ...] = ()


BACKENDS: dict[str, Backend] = {
    "antigravity_cli": Backend(
        "antigravity_cli", "Antigravity", "Antigravity CLI (agy)", cli="agy",
        default_model="gemini-3.8-flash-high",
        models=(
            ("gemini-3.8-flash-high", "Gemini 3.8 Flash (High)"),
            ("gemini-3.8-flash-medium", "Gemini 3.8 Flash (Medium)"),
            ("gemini-3.8-flash-low", "Gemini 3.8 Flash (Low)"),
            ("gemini-3.7-flash-high", "Gemini 3.7 Flash (High)"),
            ("gemini-3.7-flash-medium", "Gemini 3.7 Flash (Medium)"),
            ("gemini-3.7-flash-low", "Gemini 3.7 Flash (Low)"),
            ("gemini-3.1-pro-high", "Gemini 3.1 Pro (High)"),
            ("gemini-3.1-pro-low", "Gemini 3.1 Pro (Low)"),
            ("claude-sonnet-4-6", "Claude Sonnet 4.6"),
            ("claude-opus-4-6-thinking", "Claude Opus 4.6 (Thinking)"),
            ("gpt-oss-120b-medium", "GPT OSS 120B (Medium)"),
        ),
    ),
    "claude_cli": Backend(
        "claude_cli", "Claude", "Claude Pro or Max subscription", cli="claude",
        install_url="https://claude.com/claude-code",
        install_command="npm install -g @anthropic-ai/claude-code",
        login_args=("auth", "login"), default_model="sonnet",
        models=(
            ("sonnet", "Claude Sonnet (Default)"),
            ("opus", "Claude Opus"),
            ("haiku", "Claude Haiku"),
            ("claude-3-7-sonnet", "Claude 3.7 Sonnet"),
            ("claude-3-5-sonnet", "Claude 3.5 Sonnet"),
            ("claude-3-5-haiku", "Claude 3.5 Haiku"),
        ),
    ),
    "codex_cli": Backend(
        "codex_cli", "ChatGPT", "ChatGPT Plus or Pro subscription", cli="codex",
        install_url="https://developers.openai.com/codex/cli",
        install_command="npm install -g @openai/codex",
        login_args=("login",), default_model="",
        models=(
            ("gpt-5.5", "GPT-5.5"),
            ("gpt-5.4", "GPT-5.4"),
            ("gpt-4o", "GPT-4o"),
            ("gpt-4o-mini", "GPT-4o mini"),
            ("o3", "o3"),
            ("o3-mini", "o3-mini"),
            ("o1", "o1"),
        ),
    ),
    "anthropic_api": Backend(
        "anthropic_api", "Anthropic API key", "Pay-as-you-go API key",
        install_url="https://console.anthropic.com/settings/keys",
        default_model="claude-sonnet-5-5",
        models=(
            ("claude-sonnet-5-5", "Claude Sonnet 5.5"),
            ("claude-3-7-sonnet-latest", "Claude 3.7 Sonnet Latest"),
            ("claude-3-5-sonnet-latest", "Claude 3.5 Sonnet Latest"),
            ("claude-3-5-haiku-latest", "Claude 3.5 Haiku Latest"),
            ("claude-3-opus-latest", "Claude 3 Opus Latest"),
        ),
    ),
    "openai_api": Backend(
        "openai_api", "OpenAI API key", "Pay-as-you-go API key",
        install_url="https://platform.openai.com/api-keys", default_model="gpt-5.5",
        models=(
            ("gpt-5.5", "GPT-5.5"),
            ("gpt-5", "GPT-5"),
            ("gpt-4.5-preview", "GPT-4.5 Preview"),
            ("gpt-4o", "GPT-4o"),
            ("gpt-4o-mini", "GPT-4o mini"),
            ("o3", "o3"),
            ("o3-mini", "o3-mini"),
            ("o1", "o1"),
        ),
    ),
}


@dataclass(frozen=True)
class Status:
    ready: bool
    title: str
    detail: str
    action: str = ""   # install | login | key | test | ""


# ------------------------------------------------------------------ discovery
def _candidate_dirs() -> list[Path]:
    """Places CLI installers use. A GUI app on macOS starts with a minimal PATH."""
    home = Path.home()
    dirs = [Path(p) for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    dirs += [home / ".local" / "bin", home / ".claude" / "local", home / ".npm-global" / "bin",
             home / ".volta" / "bin", home / ".bun" / "bin", home / ".cargo" / "bin"]
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        local = os.environ.get("LOCALAPPDATA", "")
        if appdata:
            dirs.append(Path(appdata) / "npm")
        if local:
            dirs += [Path(local) / "Programs" / "claude", Path(local) / "Microsoft" / "WinGet" / "Links"]
    else:
        dirs += [Path("/opt/homebrew/bin"), Path("/usr/local/bin"), Path("/usr/bin"), Path("/snap/bin")]
        nvm = home / ".nvm" / "versions" / "node"
        if nvm.is_dir():
            dirs += sorted((p / "bin" for p in nvm.iterdir()), reverse=True)
        dirs += _login_shell_path()
    seen, out = set(), []
    for directory in dirs:
        key = str(directory)
        if key not in seen:
            seen.add(key)
            out.append(directory)
    return out


@lru_cache(maxsize=1)
def _login_shell_path() -> list[Path]:
    """The PATH a Terminal window would have (macOS/Linux), read once."""
    shell = os.environ.get("SHELL") or ("/bin/zsh" if sys.platform == "darwin" else "/bin/bash")
    if not Path(shell).is_file():
        return []
    try:
        result = subprocess.run([shell, "-ilc", "printf '%s' \"$PATH\""], capture_output=True, text=True,
                                timeout=4, stdin=subprocess.DEVNULL)
        return [Path(p) for p in result.stdout.strip().split(os.pathsep) if p.startswith("/")]
    except (OSError, subprocess.SubprocessError):
        return []


def find_cli(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    suffixes = (".cmd", ".exe", ".bat", "") if sys.platform == "win32" else ("",)
    for directory in _candidate_dirs():
        for suffix in suffixes:
            candidate = directory / f"{name}{suffix}"
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


def _env_for(binary: str) -> dict[str, str]:
    """Run a CLI with its own directory (and node) on PATH, outside any agent session."""
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
    extra = [str(Path(binary).parent)] + [str(p) for p in _candidate_dirs()]
    env["PATH"] = os.pathsep.join(dict.fromkeys(extra + env.get("PATH", "").split(os.pathsep)))
    return env


# ------------------------------------------------------------------ keys
def _keyring():
    from .apply.email_verification import _keyring as vault
    return vault()


def save_api_key(backend: str, key: str) -> None:
    if backend not in ("anthropic_api", "openai_api"):
        raise ValueError("Only API-key connections store a key")
    key = key.strip()
    if not key or any(ch.isspace() for ch in key) or len(key) > 400:
        raise ValueError("Paste the API key exactly as shown by the provider")
    _keyring().set_password(KEY_SERVICE, backend, key)


def delete_api_key(backend: str) -> None:
    vault = _keyring()
    try:
        vault.delete_password(KEY_SERVICE, backend)
    except vault.errors.PasswordDeleteError:
        pass


def _api_key(backend: str) -> str:
    env_name = {"anthropic_api": "ANTHROPIC_API_KEY", "openai_api": "OPENAI_API_KEY"}[backend]
    if os.environ.get(env_name):
        return os.environ[env_name]
    try:
        return _keyring().get_password(KEY_SERVICE, backend) or ""
    except Exception:  # noqa: BLE001 - a locked vault means "not connected"
        return ""


# ------------------------------------------------------------------ status
def status(backend: str) -> Status:
    """A local, fast readiness check. It never sends a prompt."""
    if backend in ("", "none"):
        return Status(False, "Not connected", "Luna is off. Connect Claude or ChatGPT to draft answers.", "")
    spec = BACKENDS.get(backend)
    if spec is None:
        return Status(False, "Unknown connection", "Choose Claude or ChatGPT.", "")
    if not spec.cli:
        if _api_key(backend):
            return Status(True, f"{spec.name} saved", "Stored on this computer, never in the database.", "test")
        return Status(False, f"Add your {spec.name}", "Paste a key to connect.", "key")
    binary = find_cli(spec.cli)
    if not binary:
        return Status(False, f"Install {spec.name} first",
                      f"Luna uses the {spec.name} app with your {spec.plan}. Install it, then sign in.", "install")
    try:
        if backend == "antigravity_cli":
            logged_in = (Path.home() / ".gemini" / "oauth_creds.json").is_file()
        elif backend == "claude_cli":
            result = subprocess.run([binary, "auth", "status"], capture_output=True, text=True, timeout=8,
                                    env=_env_for(binary), stdin=subprocess.DEVNULL)
            try:
                logged_in = bool(json.loads(result.stdout or "{}").get("loggedIn"))
            except json.JSONDecodeError:
                logged_in = result.returncode == 0 and "logged in" in result.stdout.lower()
        else:
            result = subprocess.run([binary, "login", "status"], capture_output=True, text=True, timeout=8,
                                    env=_env_for(binary), stdin=subprocess.DEVNULL)
            text = (result.stdout + result.stderr).lower()
            logged_in = result.returncode == 0 and "not logged in" not in text
    except (OSError, subprocess.SubprocessError):
        logged_in = False
    if not logged_in:
        return Status(False, f"Sign in to {spec.name}",
                      f"Sign in once with your {spec.plan}. A Terminal window will guide you.", "login")
    return Status(True, f"{spec.name} connected", f"Using your {spec.plan}.", "test")


def login_command(backend: str) -> list[str]:
    spec = BACKENDS[backend]
    binary = find_cli(spec.cli) or spec.cli
    return [binary, *spec.login_args]


def open_login(backend: str) -> str:
    """Open a terminal running the provider's sign-in. Returns the command for display."""
    command = login_command(backend)
    shown = " ".join(f'"{part}"' if " " in part else part for part in command)
    if sys.platform == "darwin":
        script = shown.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.Popen(["osascript", "-e", f'tell application "Terminal" to do script "{script}"',
                          "-e", 'tell application "Terminal" to activate'])
    elif sys.platform == "win32":
        subprocess.Popen(["cmd", "/c", "start", "cmd", "/k", *command])
    else:
        for terminal in ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm"):
            path = shutil.which(terminal)
            if path:
                args = [path, "--", *command] if terminal == "gnome-terminal" else [path, "-e", shown]
                subprocess.Popen(args)
                break
        else:
            raise RuntimeError(f"Open a terminal and run: {shown}")
    return shown


# ------------------------------------------------------------------ calls
class AgentError(RuntimeError):
    pass


def _scratch() -> Path:
    directory = Path(config.AGENT_SCRATCH)
    directory.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="run-", dir=directory))


# Preparation and AI Autofill fan out (parallel_prepare x parallel_agent_calls); this
# ceiling keeps the number of agent processes alive at once bounded.
MAX_AGENT_CALLS = 8
_call_slots = threading.BoundedSemaphore(MAX_AGENT_CALLS)
_calls_running = 0
_calls_lock = threading.Lock()
_calls_changed = threading.Condition(_calls_lock)


def complete(prompt: str, settings: dict, *, timeout: int = 120) -> str:
    """Send one prompt to the connected agent and return its text reply."""
    global _calls_running
    from .governor import wait_for_headroom
    with _call_slots:
        # Past the first call, wait while the computer is busy so it stays responsive.
        wait_for_headroom(lambda: _calls_running, max_wait=300)
        from .jarvis import active
        limit = MAX_AGENT_CALLS // 2 if active(settings) else MAX_AGENT_CALLS
        with _calls_changed:
            while _calls_running >= limit:
                _calls_changed.wait(timeout=1)
            _calls_running += 1
        try:
            from .governor import busy
            if active(settings) and busy():
                raise AgentError("JARVIS deferred this AI call while the computer is busy")
            return _complete(prompt, settings, timeout)
        finally:
            with _calls_changed:
                _calls_running -= 1
                _calls_changed.notify_all()


def _complete(prompt: str, settings: dict, timeout: int) -> str:
    agent = settings.get("agent") or {}
    backend = agent.get("backend", "none")
    model = (agent.get("model") or "").strip() or BACKENDS.get(backend, Backend("", "", "")).default_model
    if backend in ("", "none"):
        raise AgentError("Luna is not connected. Connect Claude or ChatGPT in Settings → Connections.")
    started = time.time()
    try:
        if backend == "claude_cli":
            return _claude_cli(prompt, model, timeout)
        if backend == "codex_cli":
            return _codex_cli(prompt, model, timeout)
        if backend == "antigravity_cli":
            return _antigravity_cli(prompt, model, timeout)
        if backend == "anthropic_api":
            return _anthropic_api(prompt, model, timeout)
        if backend == "openai_api":
            return _openai_api(prompt, model, timeout)
    except subprocess.TimeoutExpired as error:
        raise AgentError(f"The agent did not answer within {int(time.time() - started)} seconds") from error
    raise AgentError(f"Unknown agent connection: {backend}")


def _claude_cli(prompt: str, model: str, timeout: int) -> str:
    binary = find_cli("claude")
    if not binary:
        raise AgentError("Claude Code is not installed")
    scratch = _scratch()
    try:
        command = [binary, "-p", "--output-format", "json", "--max-turns", "1", "--safe-mode", "--tools", "",
                   "--disable-slash-commands", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                   "--setting-sources", "", "--no-session-persistence"]
        if model:
            command += ["--model", model]
        result = subprocess.run(command, input=prompt, capture_output=True, text=True, timeout=timeout,
                                env=_env_for(binary), cwd=str(scratch))
        if result.returncode:
            detail = (result.stderr or result.stdout).strip().splitlines()[-1:] or [""]
            raise AgentError(f"Claude stopped (exit {result.returncode}). {detail[0][:200]}")
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise AgentError("Claude returned an unreadable reply") from error
        if data.get("is_error"):
            raise AgentError(str(data.get("result") or "Claude reported an error")[:300])
        return str(data.get("result") or "")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _codex_cli(prompt: str, model: str, timeout: int) -> str:
    binary = find_cli("codex")
    if not binary:
        raise AgentError("Codex is not installed")
    scratch = _scratch()
    output = scratch / "reply.txt"
    try:
        command = [binary, "exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config",
                   "--sandbox", "read-only", "--color", "never",
                   "--disable", "shell_tool", "--disable", "unified_exec", "--disable", "apps",
                   "--disable", "browser_use", "--disable", "computer_use", "-c", 'web_search="disabled"',
                   "-C", str(scratch), "-o", str(output)]
        if model:
            command += ["-m", model]
        command.append("-")
        result = subprocess.run(command, input=prompt, capture_output=True, text=True, timeout=timeout,
                                env=_env_for(binary), cwd=str(scratch))
        if result.returncode:
            lines = [line for line in (result.stderr or result.stdout).strip().splitlines() if line.strip()]
            raise AgentError(f"Codex stopped (exit {result.returncode}). {(lines[-1] if lines else '')[:200]}")
        if not output.is_file():
            raise AgentError("Codex returned no reply")
        return output.read_text(encoding="utf-8", errors="replace")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


_AGY_SIGN_IN = ("antigravity-oauth-token", "installation_id")
_AGY_HISTORY = ("annotations", "brain", "conversations", "implicit", "presence", "log", "crashes")
_agy_home_lock = threading.Lock()
_agy_home_ready = False


def _agy_home() -> Path | None:
    """A private agy home holding only the candidate's sign-in, linked from their own.

    agy starts every MCP server in the user's config on every call (often a dozen,
    ~35 s and hundreds of MB each time) and offers no per-call switch. Starployment
    needs plain text replies, so its calls run with this home: no MCP servers, no
    plugins, and the user's own agy config is never changed. ``None`` when agy has
    no saved sign-in to link, so the call runs as before."""
    global _agy_home_ready
    real = Path.home() / ".gemini" / "antigravity-cli"
    if not (real / "antigravity-oauth-token").is_file():
        return None
    home = Path(config.AGENT_HOME) / "agy"
    state = home / ".gemini" / "antigravity-cli"
    with _agy_home_lock:
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in _AGY_SIGN_IN:
            source, link = real / name, state / name
            if source.exists() and not (link.is_symlink() and link.resolve() == source.resolve()):
                link.unlink(missing_ok=True)
                link.symlink_to(source)
        if not _agy_home_ready:   # once per launch: drop the conversations earlier calls left behind
            for name in _AGY_HISTORY:
                shutil.rmtree(state / name, ignore_errors=True)
            _agy_home_ready = True
    return home


def _antigravity_cli(prompt: str, model: str, timeout: int) -> str:
    binary = find_cli("agy")
    if not binary:
        raise AgentError("Antigravity CLI (agy) is not installed")
    scratch = _scratch()
    try:
        command = [binary, "-p", prompt, "--output-format", "json", "--sandbox", "--disable-slash-commands"]
        if model:
            command += ["--model", model]
        env = _env_for(binary)
        try:
            home = _agy_home()
        except OSError:
            home = None
        if home is not None:
            env["HOME"] = str(home)
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                                env=env, cwd=str(scratch))
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            if result.returncode:
                detail = (result.stderr or result.stdout).strip().splitlines()[-1:] or [""]
                raise AgentError(f"Antigravity stopped (exit {result.returncode}). {detail[0][:200]}")
            raise AgentError("Antigravity returned an unreadable reply") from error
        if data.get("status") == "ERROR":
            raise AgentError(str(data.get("error") or "Antigravity reported an error"))
        if result.returncode:
            detail = (result.stderr or result.stdout).strip().splitlines()[-1:] or [""]
            raise AgentError(f"Antigravity stopped (exit {result.returncode}). {detail[0][:200]}")
        return str(data.get("response") or "")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _anthropic_api(prompt: str, model: str, timeout: int) -> str:
    key = _api_key("anthropic_api")
    if not key:
        raise AgentError("No Anthropic API key is saved")
    import anthropic
    client = anthropic.Anthropic(api_key=key, timeout=timeout, max_retries=1)
    message = client.messages.create(model=model, max_tokens=4000, messages=[{"role": "user", "content": prompt}])
    return "".join(getattr(block, "text", "") for block in message.content)


def _openai_api(prompt: str, model: str, timeout: int) -> str:
    key = _api_key("openai_api")
    if not key:
        raise AgentError("No OpenAI API key is saved")
    import httpx
    response = httpx.post("https://api.openai.com/v1/chat/completions", timeout=timeout,
                          headers={"Authorization": f"Bearer {key}"},
                          json={"model": model, "messages": [{"role": "user", "content": prompt}]})
    if response.status_code >= 400:
        raise AgentError(f"OpenAI returned HTTP {response.status_code}: {response.text[:200]}")
    return str(response.json()["choices"][0]["message"]["content"] or "")


def test_connection(settings: dict) -> tuple[bool, str]:
    """Send a tiny prompt; returns (ok, message) for the Connections panel."""
    started = time.time()
    try:
        reply = complete("Reply with exactly one word: ready", settings, timeout=90)
    except Exception as error:  # noqa: BLE001
        return False, str(error)[:300]
    if "ready" in reply.lower():
        return True, f"Luna is ready ({time.time() - started:.0f}s round trip)."
    return False, "The agent answered, but not as expected. Try again or choose another connection."
