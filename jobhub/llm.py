"""LLM abstraction with pluggable backends. The rest of the app must work with `LLM.available() == False`.

Auto selects only a configured Anthropic key. Claude CLI is explicitly opt-in and tool-free.
Codex CLI is not used for untrusted resume/job text because its tools cannot be reliably disabled.
Every call is a single prompt -> text (optionally parsed as JSON). No tools, no agents.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from typing import Any

from . import config

_SCRATCH = config.DATA / "llm_scratch"
_SCRATCH.mkdir(exist_ok=True)


def _clean_env() -> dict[str, str]:
    """Env for `claude -p`: drop nesting markers so it runs from inside another Claude Code session."""
    return {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}




def extract_json(text: str) -> Any:
    """Pull the first JSON object/array out of a model reply (tolerates ``` fences and prose)."""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0] or [0])
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc: esc = False
            elif ch == "\\": esc = True
            elif ch == '"': in_str = False
            continue
        if ch == '"': in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    return json.loads(text)


class LLM:
    def __init__(self, backend: str | None = None):
        self.requested = backend or config.LLM_BACKEND
        self.backend: str | None = None
        self.calls = 0
        self.seconds = 0.0
        self.last_error = ""
        self._detect()

    # ---------- detection ----------
    def _detect(self) -> None:
        # Availability checks must never make paid calls or block the dashboard.
        if self.requested in {"auto", "anthropic"} and config.ANTHROPIC_API_KEY:
            self.backend = "anthropic"
        elif self.requested == "claude_cli" and shutil.which("claude"):
            self.backend = "claude_cli"
        elif self.requested == "codex_cli":
            self.last_error = "Codex CLI is disabled for resume parsing: a tool-free execution mode is required."
        elif self.requested not in {"auto", "none", "anthropic", "claude_cli"}:
            self.last_error = "Unsupported LLM backend"

    def available(self) -> bool:
        return self.backend is not None

    def describe(self) -> dict[str, Any]:
        return {"backend": self.backend or "none", "calls": self.calls, "seconds": round(self.seconds, 1),
                "last_error": self.last_error}

    # ---------- calls ----------
    def complete(self, prompt: str, system: str | None = None, timeout: int = 45) -> str:
        if not self.backend:
            raise RuntimeError("no LLM backend available")
        full = f"{system}\n\n{prompt}" if system else prompt
        t = time.time()
        try:
            return self._call(self.backend, full, timeout=max(1, min(timeout, 45)))
        finally:
            self.calls += 1
            self.seconds += time.time() - t

    def json(self, prompt: str, system: str | None = None, timeout: int = 45) -> Any:
        sys_ = (system or "") + "\nRespond with valid JSON only. No prose, no markdown fences."
        out = self.complete(prompt, sys_.strip(), timeout=timeout)
        # One bounded request. A JSON retry doubles latency and spending.
        return extract_json(out)

    def _call(self, backend: str, prompt: str, timeout: int) -> str:
        if backend == "anthropic":
            import anthropic  # lazy
            client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, timeout=timeout, max_retries=0)
            msg = client.messages.create(model=config.ANTHROPIC_MODEL, max_tokens=4000,
                                         messages=[{"role": "user", "content": prompt}])
            return "".join(getattr(b, "text", "") for b in msg.content)
        if backend == "claude_cli":
            p = subprocess.run(
                ["claude", "-p", "--output-format", "json", "--model", config.CLAUDE_CLI_MODEL, "--max-turns", "1",
                 "--safe-mode", "--tools", "", "--disable-slash-commands", "--strict-mcp-config",
                 "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "", "--no-session-persistence"],
                input=prompt, capture_output=True, text=True, timeout=timeout, env=_clean_env(), cwd=str(_SCRATCH))
            if p.returncode:
                raise RuntimeError(f"Claude CLI failed (exit {p.returncode})")
            try:
                d = json.loads(p.stdout)
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"claude cli bad output: {p.stdout[:200]} {p.stderr[:200]}") from e
            if d.get("is_error"):
                raise RuntimeError(d.get("result", "claude cli error"))
            return d.get("result", "")
        if backend == "codex_cli":
            raise RuntimeError("Codex CLI candidate-data calls are disabled until a tool-free mode is available")
        raise RuntimeError(f"unknown backend {backend}")


_singleton: LLM | None = None


def get_llm() -> LLM:
    global _singleton
    if _singleton is None:
        _singleton = LLM()
    return _singleton
