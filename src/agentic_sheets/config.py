"""Runtime configuration, loaded from environment / .env via pydantic-settings."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

EffortLevel = Literal["low", "medium", "high", "xhigh", "max"]
GoogleAuthMode = Literal["service_account", "oauth", "disabled"]
LLMProvider = Literal["auto", "groq", "anthropic", "openai", "openrouter", "together", "ollama", "custom"]

#: Defaults per provider: (base_url, default model, key env var).
#: Everything except `anthropic` speaks the OpenAI chat-completions wire format,
#: so one adapter serves them all — only these three values differ.
PROVIDER_DEFAULTS: dict[str, tuple[str | None, str, str]] = {
    "groq": (
        "https://api.groq.com/openai/v1",
        "llama-3.3-70b-versatile",
        "GROQ_API_KEY",
    ),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini", "OPENAI_API_KEY"),
    "openrouter": (
        "https://openrouter.ai/api/v1",
        "meta-llama/llama-3.3-70b-instruct",
        "OPENROUTER_API_KEY",
    ),
    "together": (
        "https://api.together.xyz/v1",
        "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "TOGETHER_API_KEY",
    ),
    "ollama": ("http://localhost:11434/v1", "llama3.1:8b", "OLLAMA_API_KEY"),
    "anthropic": (None, "claude-opus-5", "ANTHROPIC_API_KEY"),
    "custom": (None, "", "LLM_API_KEY"),
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- LLM provider ------------------------------------------------------
    #: "auto" picks the first provider that has a key configured, preferring the
    #: free option (Groq) so a fresh clone runs at zero cost.
    llm_provider: LLMProvider = "auto"
    llm_model: Optional[str] = None            # blank -> the provider's default
    llm_base_url: Optional[str] = None         # blank -> the provider's default
    llm_api_key: Optional[str] = None          # generic override / `custom`
    llm_disable_parallel_tool_calls: bool = True

    anthropic_api_key: Optional[str] = None
    groq_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    openrouter_api_key: Optional[str] = None
    together_api_key: Optional[str] = None
    ollama_api_key: str = "ollama"             # Ollama ignores the key but the SDK requires one

    # ---- Agent loop --------------------------------------------------------
    agent_model: Optional[str] = None          # legacy alias for llm_model

    #: Deliberately modest. Providers reserve `max_tokens` against their
    #: rate-limit budget rather than charging actual output, so an
    #: over-provisioned value gets requests rejected before they are even read.
    #: Measured on Groq: a 1,315-token prompt with max_tokens=8000 was counted
    #: as 9,315 and rejected against an 8,000/minute cap; the identical prompt
    #: at 4,000 succeeded. The agent's real outputs are a tool call (~100
    #: tokens) or the final report (~1,000), so 4,096 is ample headroom.
    agent_max_tokens: int = 4_096
    agent_effort: Optional[EffortLevel] = None  # Anthropic only; ignored elsewhere
    agent_max_iterations: int = 25
    agent_planning: bool = True

    # ---- Paths -------------------------------------------------------------
    workspace_dir: Path = Path("./workspace")
    log_dir: Path = Path("./logs")
    memory_dir: Path = Path("./.agent_memory")
    tools_config: Path = Path("./config/tools.yaml")

    # ---- Logging -----------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = False

    # ---- Excel -------------------------------------------------------------
    excel_visible: bool = True
    excel_keep_open: bool = True

    # ---- Google Sheets -----------------------------------------------------
    google_auth_mode: GoogleAuthMode = "service_account"
    google_credentials_file: Path = Path("./credentials/service_account.json")
    google_oauth_client_file: Path = Path("./credentials/oauth_client.json")
    google_token_file: Path = Path("./credentials/token.json")
    google_share_with_email: Optional[str] = None
    google_default_spreadsheet_title: str = "Employee Data (Agent Import)"
    google_spreadsheet_id: Optional[str] = None

    # ---- Retry -------------------------------------------------------------
    tool_max_retries: int = Field(default=2, ge=0, le=6)
    tool_retry_base_delay: float = Field(default=1.0, ge=0.0)

    @field_validator("agent_effort", "llm_model", "llm_base_url", "agent_model", mode="before")
    @classmethod
    def _blank_is_none(cls, value):
        # An empty `KEY=` line in .env should mean "use the default", not "".
        if isinstance(value, str) and not value.strip():
            return None
        return value

    # ---- provider resolution ----------------------------------------------

    def resolved_provider(self) -> str:
        """Which provider to actually use.

        `auto` prefers the free tier so a fresh clone works at zero cost, then
        falls back to whatever key is present.
        """
        if self.llm_provider != "auto":
            return self.llm_provider
        for provider, key in (
            ("groq", self.groq_api_key),
            ("anthropic", self.anthropic_api_key),
            ("openai", self.openai_api_key),
            ("openrouter", self.openrouter_api_key),
            ("together", self.together_api_key),
        ):
            if key:
                return provider
        # Nothing configured: name Groq so the error message points at the free option.
        return "groq"

    def expected_key_env_var(self) -> str:
        provider = self.resolved_provider()
        return PROVIDER_DEFAULTS.get(provider, (None, "", "LLM_API_KEY"))[2]

    def resolved_llm_api_key(self) -> Optional[str]:
        if self.llm_api_key:
            return self.llm_api_key
        return {
            "groq": self.groq_api_key,
            "anthropic": self.anthropic_api_key,
            "openai": self.openai_api_key,
            "openrouter": self.openrouter_api_key,
            "together": self.together_api_key,
            "ollama": self.ollama_api_key,
        }.get(self.resolved_provider())

    def resolved_llm_base_url(self) -> Optional[str]:
        if self.llm_base_url:
            return self.llm_base_url
        return PROVIDER_DEFAULTS.get(self.resolved_provider(), (None, "", ""))[0]

    def resolved_llm_model(self) -> str:
        # `AGENT_MODEL` is kept as an alias so existing .env files keep working.
        return (
            self.llm_model
            or self.agent_model
            or PROVIDER_DEFAULTS.get(self.resolved_provider(), (None, "", ""))[1]
        )

    def uses_anthropic(self) -> bool:
        return self.resolved_provider() == "anthropic"

    def ensure_directories(self) -> None:
        for directory in (self.workspace_dir, self.log_dir, self.memory_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def redacted(self) -> dict:
        data = self.model_dump(mode="json")
        for field in data:
            if field.endswith("api_key") and data[field]:
                data[field] = "***redacted***"
        return data


_settings: Settings | None = None


def get_settings(refresh: bool = False) -> Settings:
    """Process-wide settings singleton."""
    global _settings
    if _settings is None or refresh:
        _settings = Settings()
    return _settings
