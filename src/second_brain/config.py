"""Settings loaded from ``config.yaml`` (gitignored) plus environment/.env.

Nothing personal is hardcoded: vault location, queue locations and the
Instagram export path all come from config. Relative paths are resolved
against the directory that contains the config file, so the pipeline behaves
the same whether it is started from the .bat file, a terminal, or a scheduler.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .taxonomy import DEFAULT_FOLDER_MAP

logger = logging.getLogger(__name__)

CONFIG_ENV_VAR = "SECOND_BRAIN_CONFIG"
DEFAULT_CONFIG_NAME = "config.yaml"

# Fixed queue file names. They are fixed (not configurable) so .gitignore can
# block them by name no matter where data_dir points.
CLEAN_METADATA_FILE = "clean_metadata.json"
ORGANIZED_CSV_FILE = "organized_tiktoks.csv"
LEDGER_DIR_NAME = ".second-brain"
LEDGER_FILE_NAME = "url_ledger.jsonl"
CRUSHER_STATE_FILE = "crusher_state.jsonl"
CRUSHER_CACHE_DIR = "crusher_cache"
CRUSHER_LOCK_FILE = "crusher.lock"
CRUSHER_REPORT_FILE = "crusher_report.md"


class ConfigError(ValueError):
    """Config file missing, unreadable, or invalid."""


@dataclass(frozen=True)
class ExtractSettings:
    workers: int = 10
    timeout_seconds: int = 20
    # Persist clean_metadata.json every N finished URLs so a crash mid-run
    # does not throw away all yt-dlp work done so far.
    checkpoint_every: int = 10
    # e.g. "chrome" / "edge"; passed to yt-dlp --cookies-from-browser.
    # Cookies are read from the local browser at runtime and never stored here.
    cookies_from_browser: str | None = None


@dataclass(frozen=True)
class GeminiSettings:
    model: str = "gemini-3.6-flash"
    batch_size: int = 100
    max_retries: int = 5
    backoff_base_seconds: float = 8.0
    jitter_seconds: tuple[float, float] = (1.0, 3.0)
    pause_between_batches_seconds: float = 10.0


@dataclass(frozen=True)
class ModelASettings:
    inbox_path: Path | None = None
    metadata_snapshot_path: Path | None = None
    batch_size: int = 10
    backoff_base_seconds: float = 15.0
    confidence_threshold: float = 0.65


@dataclass(frozen=True)
class CrusherSettings:
    """Multimodal re-analysis of existing vault cards (``second-brain crush``).

    Quota fields use ``null`` in YAML to mean unlimited (rely on 429 backoff).
    Keys are read only from ``api_key_env_vars`` in the environment, never from
    this file.
    """

    model: str = "gemini-3.6-flash"
    fallback_model: str | None = None
    batch_size: int = 10
    videos_per_request: int = 1
    requests_per_minute: int | None = None
    requests_per_day: int | None = None
    tokens_per_minute: int | None = None
    api_key_env_vars: tuple[str, ...] = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
    video_fps: float = 1.0
    short_video_fps: float = 2.0
    short_video_max_seconds: float = 30.0
    media_resolution: str = "MEDIA_RESOLUTION_LOW"
    max_video_seconds: float = 180.0
    max_download_mb: float = 80.0
    inline_max_mb: float = 18.0
    scene_threshold: float = 0.35
    max_passes: int = 3
    confidence_threshold: float = 0.55
    include_folders: tuple[str, ...] = ()
    skip_statuses: tuple[str, ...] = ("tossed",)
    prompt_version: str = "1"
    geocoder: str = "nominatim"
    geocoder_user_agent: str = "obsidian-second-brain-pipeline/0.1"
    cookies_from_browser: str | None = None
    backoff_base_seconds: float = 15.0
    file_poll_seconds: float = 2.0
    file_poll_max_wait_seconds: float = 120.0
    lock_stale_hours: float = 6.0


@dataclass(frozen=True)
class Settings:
    vault_path: Path
    data_dir: Path
    pending_links_path: Path
    instagram_export_path: Path
    ledger_path: Path
    resources_subdir: str = "3 - Resources"
    extract: ExtractSettings = field(default_factory=ExtractSettings)
    gemini: GeminiSettings = field(default_factory=GeminiSettings)
    model_a: ModelASettings = field(default_factory=ModelASettings)
    crusher: CrusherSettings = field(default_factory=CrusherSettings)
    folder_map: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_FOLDER_MAP))

    @property
    def resources_dir(self) -> Path:
        return self.vault_path / self.resources_subdir

    @property
    def clean_metadata_path(self) -> Path:
        return self.data_dir / CLEAN_METADATA_FILE

    @property
    def organized_csv_path(self) -> Path:
        return self.data_dir / ORGANIZED_CSV_FILE

    @property
    def model_a_inbox_path(self) -> Path:
        # Original Model A read its queue from inside the vault Inbox folder.
        return self.model_a.inbox_path or (self.resources_dir / "Inbox" / "pending_links.txt")

    @property
    def second_brain_dir(self) -> Path:
        return self.vault_path / LEDGER_DIR_NAME

    @property
    def crusher_state_path(self) -> Path:
        return self.second_brain_dir / CRUSHER_STATE_FILE

    @property
    def crusher_cache_dir(self) -> Path:
        return self.second_brain_dir / CRUSHER_CACHE_DIR

    @property
    def crusher_lock_path(self) -> Path:
        return self.second_brain_dir / CRUSHER_LOCK_FILE

    @property
    def crusher_report_path(self) -> Path:
        return self.data_dir / CRUSHER_REPORT_FILE

    def require_vault(self) -> None:
        """Fail fast with a readable message instead of silently creating a new vault."""
        if not self.vault_path.is_dir():
            raise ConfigError(
                f"vault_path does not exist or is not a folder: {self.vault_path}. "
                "Set vault_path in config.yaml to your Obsidian vault root."
            )


def _resolve(base: Path, value: Any, name: str) -> Path:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ConfigError(f"'{name}' is required in config.yaml")
    path = Path(os.path.expandvars(os.path.expanduser(str(value))))
    return path if path.is_absolute() else (base / path).resolve()


def _optional_path(base: Path, value: Any, name: str) -> Path | None:
    return None if value in (None, "") else _resolve(base, value, name)


def _section(raw: dict, name: str) -> dict:
    value = raw.get(name) or {}
    if not isinstance(value, dict):
        raise ConfigError(f"'{name}' must be a mapping in config.yaml")
    return value


def _build_dataclass(cls, values: dict, section: str):
    """Construct a settings dataclass, rejecting unknown keys (typo protection)."""
    known = set(cls.__dataclass_fields__)
    unknown = set(values) - known
    if unknown:
        raise ConfigError(f"Unknown key(s) in '{section}': {', '.join(sorted(unknown))}")
    try:
        return cls(**values)
    except TypeError as exc:
        raise ConfigError(f"Invalid '{section}' settings: {exc}") from exc


def find_config_path(explicit: str | os.PathLike | None = None) -> Path:
    """Pick the config file: --config flag, then $SECOND_BRAIN_CONFIG, then ./config.yaml."""
    if explicit:
        return Path(explicit)
    env_value = os.environ.get(CONFIG_ENV_VAR)
    if env_value:
        return Path(env_value)
    return Path.cwd() / DEFAULT_CONFIG_NAME


def load_settings(config_path: str | os.PathLike | None = None) -> Settings:
    path = find_config_path(config_path)
    if not path.is_file():
        raise ConfigError(
            f"Config file not found: {path}. Copy config.example.yaml to config.yaml and edit it."
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Could not parse {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")
    logger.debug("Loaded config from %s", path)
    return settings_from_dict(raw, base_dir=path.resolve().parent)


def settings_from_dict(raw: dict, base_dir: Path) -> Settings:
    data_dir = _resolve(base_dir, raw.get("data_dir", "./data"), "data_dir")
    vault_path = _resolve(base_dir, raw.get("vault_path"), "vault_path")

    gemini_raw = dict(_section(raw, "gemini"))
    if "jitter_seconds" in gemini_raw:
        jitter = gemini_raw["jitter_seconds"]
        if not (isinstance(jitter, (list, tuple)) and len(jitter) == 2 and jitter[0] <= jitter[1]):
            raise ConfigError("gemini.jitter_seconds must be [min, max] with min <= max")
        gemini_raw["jitter_seconds"] = (float(jitter[0]), float(jitter[1]))
    gemini = _build_dataclass(GeminiSettings, gemini_raw, "gemini")
    if not 1 <= gemini.batch_size <= 500:
        raise ConfigError("gemini.batch_size must be between 1 and 500")
    if gemini.max_retries < 1:
        raise ConfigError("gemini.max_retries must be >= 1")

    model_a_raw = dict(_section(raw, "model_a"))
    for key in ("inbox_path", "metadata_snapshot_path"):
        if key in model_a_raw:
            model_a_raw[key] = _optional_path(base_dir, model_a_raw[key], f"model_a.{key}")

    folder_map = dict(DEFAULT_FOLDER_MAP)
    taxonomy_raw = raw.get("taxonomy")
    if taxonomy_raw is not None:
        if not isinstance(taxonomy_raw, dict) or not all(isinstance(v, str) for v in taxonomy_raw.values()):
            raise ConfigError("taxonomy must map category names to folder names")
        # A config taxonomy replaces the default so a user can also remove categories.
        folder_map = {str(k): v for k, v in taxonomy_raw.items()}
    for folder in folder_map.values():
        if "/" in folder or "\\" in folder or folder in {"", ".", ".."}:
            raise ConfigError(f"taxonomy folder names must be plain folder names, got {folder!r}")

    extract = _build_dataclass(ExtractSettings, _section(raw, "extract"), "extract")

    crusher_raw = dict(_section(raw, "crusher"))
    # Users already set the model and browser cookies for the rest of the pipeline.
    # Crush only overrides them when crusher.model / crusher.cookies_from_browser are set.
    if "model" not in crusher_raw:
        crusher_raw["model"] = gemini.model
    if "cookies_from_browser" not in crusher_raw and extract.cookies_from_browser:
        crusher_raw["cookies_from_browser"] = extract.cookies_from_browser
    for list_key in ("api_key_env_vars", "include_folders", "skip_statuses"):
        if list_key in crusher_raw and crusher_raw[list_key] is not None:
            crusher_raw[list_key] = tuple(str(x) for x in crusher_raw[list_key])
    for nullable_int in ("requests_per_minute", "requests_per_day", "tokens_per_minute"):
        if nullable_int in crusher_raw and crusher_raw[nullable_int] == "":
            crusher_raw[nullable_int] = None
    crusher = _build_dataclass(CrusherSettings, crusher_raw, "crusher")
    if crusher.batch_size < 1:
        raise ConfigError("crusher.batch_size must be >= 1")
    if crusher.videos_per_request < 1:
        raise ConfigError("crusher.videos_per_request must be >= 1")
    if crusher.max_passes < 1:
        raise ConfigError("crusher.max_passes must be >= 1")

    return Settings(
        vault_path=vault_path,
        data_dir=data_dir,
        pending_links_path=_resolve(base_dir, raw.get("pending_links_path", data_dir / "pending_links.txt"), "pending_links_path"),
        instagram_export_path=_resolve(base_dir, raw.get("instagram_export_path", data_dir / "saved_posts.json"), "instagram_export_path"),
        # The ledger defaults to a hidden folder inside the vault: it is part of
        # the registry, so it should travel and be backed up with the vault.
        # Obsidian does not index dot-folders, so it never shows up as a note.
        ledger_path=_resolve(base_dir, raw.get("ledger_path") or vault_path / LEDGER_DIR_NAME / LEDGER_FILE_NAME, "ledger_path"),
        resources_subdir=str(raw.get("resources_subdir", "3 - Resources")),
        extract=extract,
        gemini=gemini,
        model_a=_build_dataclass(ModelASettings, model_a_raw, "model_a"),
        crusher=crusher,
        folder_map=folder_map,
    )
