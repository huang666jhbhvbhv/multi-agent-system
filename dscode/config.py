from dataclasses import dataclass
import os
from pathlib import Path

APP = Path(__file__).resolve().parents[1]


def load_env(path=APP / ".env"):
    """Read application-local configuration; never execute an env file."""
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


@dataclass
class Config:
    workspace: Path
    key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    thinking: str = "disabled"
    permission: str = "ask"
    max_steps: int = 40
    context_chars: int = 70000
    timeout: int = 120
    max_tokens: int = 8192
    token_budget: int = 200000
    mcp_file: Path = APP / "mcp.json"

    @classmethod
    def from_env(cls, workspace, permission="ask"):
        load_env()
        return cls(
            workspace=Path(workspace).resolve(),
            key=os.getenv("DEEPSEEK_API_KEY", ""),
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
            model=os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
            thinking=os.getenv("DEEPSEEK_THINKING", "disabled"),
            permission=permission,
            max_steps=int(os.getenv("AGENT_MAX_STEPS", "40")),
            context_chars=int(os.getenv("AGENT_CONTEXT_CHARS", "70000")),
            timeout=int(os.getenv("AGENT_TIMEOUT", "120")),
            max_tokens=int(os.getenv("AGENT_MAX_TOKENS", "8192")),
            token_budget=int(os.getenv("AGENT_TOKEN_BUDGET", "200000")),
        )
