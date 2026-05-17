"""
Central configuration system.
All settings are pydantic-validated and can be overridden via environment variables or .env file.
"""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).parent.parent


class LLMProvider(str, Enum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class UIBackend(str, Enum):
    UIA = "uia"
    WIN32 = "win32"


class OCRBackend(str, Enum):
    EASYOCR = "easyocr"
    TESSERACT = "tesseract"
    BOTH = "both"


class LogLevel(str, Enum):
    TRACE = "TRACE"
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class LLMConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LLM_", env_file=".env", extra="ignore")

    provider: LLMProvider = LLMProvider.ANTHROPIC
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")

    # Anthropic models
    claude_model: str = "claude-sonnet-4-6"
    claude_model_fast: str = "claude-haiku-4-5-20251001"

    # OpenAI fallback
    openai_model: str = "gpt-4o"

    max_tokens: int = 8192
    temperature: float = 0.1  # near-deterministic for automation
    timeout_seconds: int = 120
    max_retries: int = 3

    @field_validator("anthropic_api_key", "openai_api_key", mode="before")
    @classmethod
    def read_from_env(cls, v: str, info) -> str:
        if not v:
            env_map = {
                "anthropic_api_key": "ANTHROPIC_API_KEY",
                "openai_api_key": "OPENAI_API_KEY",
            }
            field_name = info.field_name if hasattr(info, "field_name") else ""
            return os.environ.get(env_map.get(field_name, ""), "")
        return v


class VisionConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VISION_", extra="ignore")

    ocr_backend: OCRBackend = OCRBackend.EASYOCR
    ocr_languages: list[str] = ["ru", "en"]
    tesseract_cmd: str = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    confidence_threshold: float = 0.75
    screenshot_format: Literal["png", "jpeg"] = "png"
    screenshot_quality: int = 95
    dpi_scale_factor: float = 1.0  # auto-detected at runtime
    roi_padding: int = 20  # pixels around detected regions
    template_match_threshold: float = 0.85


class UIConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="UI_", extra="ignore")

    backend: UIBackend = UIBackend.UIA
    action_delay_ms: int = 150  # base delay between actions
    click_delay_ms: int = 100
    type_interval_seconds: float = 0.03
    wait_timeout_seconds: int = 30
    find_element_timeout_seconds: int = 15
    focus_wait_ms: int = 500
    animation_wait_ms: int = 300


class RecoveryConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RECOVERY_", extra="ignore")

    watchdog_interval_seconds: int = 5
    hang_threshold_seconds: int = 60
    max_crash_restarts: int = 3
    restart_wait_seconds: int = 8
    popup_check_interval_seconds: int = 3
    max_action_retries: int = 5
    circuit_breaker_threshold: int = 10
    circuit_breaker_reset_seconds: int = 60


class StorageConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STORAGE_", extra="ignore")

    db_path: Path = PROJECT_ROOT / "storage" / "agent.db"
    checkpoint_dir: Path = PROJECT_ROOT / "storage" / "checkpoints"
    screenshot_dir: Path = PROJECT_ROOT / "logs" / "screenshots"
    report_output_dir: Path = PROJECT_ROOT / "reports"


class ReportConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="REPORT_", extra="ignore")

    template_path: Path = PROJECT_ROOT / "report" / "templates" / "lab_template.docx"
    author: str = "Autonomous Lab Agent"
    university: str = ""
    department: str = ""
    font_name: str = "Times New Roman"
    font_size_body: int = 14
    font_size_heading1: int = 16
    font_size_heading2: int = 14
    line_spacing: float = 1.5
    page_margin_cm: float = 2.0
    figure_caption_prefix: str = "Рисунок"
    table_caption_prefix: str = "Таблица"


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Sub-configs
    llm: LLMConfig = Field(default_factory=LLMConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    recovery: RecoveryConfig = Field(default_factory=RecoveryConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)

    # Logging
    log_level: LogLevel = LogLevel.INFO
    log_dir: Path = PROJECT_ROOT / "logs"
    log_rotation: str = "50 MB"
    log_retention: str = "30 days"

    # Runtime
    dry_run: bool = False
    max_concurrent_tasks: int = 1
    session_id: str = ""

    def ensure_dirs(self) -> None:
        """Create all required directories at startup."""
        dirs = [
            self.log_dir,
            self.storage.db_path.parent,
            self.storage.checkpoint_dir,
            self.storage.screenshot_dir,
            self.storage.report_output_dir,
        ]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)


_config_instance: AppConfig | None = None


def get_config() -> AppConfig:
    global _config_instance
    if _config_instance is None:
        _config_instance = AppConfig()
    return _config_instance


def reset_config() -> None:
    """Force re-read config (for testing)."""
    global _config_instance
    _config_instance = None
