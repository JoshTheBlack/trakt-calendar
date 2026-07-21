"""Front-end-editable configuration (requirement C).

Settings persist to data/settings.json (git-ignored). Everything that used to be
a hardcoded PHP variable — including the Trakt API credentials — now lives here and
is editable from the Settings modal in the UI.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

DATA_DIR = Path(os.environ.get("TRAKT_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
SETTINGS_FILE = DATA_DIR / "settings.json"

# Default genre/country filters carried over from the original PHP script.
DEFAULT_GENRES = "-animation,-anime,-children,-game-show,-home-and-garden,-music,-reality,-special-interest,-talk-show"
DEFAULT_COUNTRIES = "ar,au,at,be,br,ca,cl,cn,co,cz,dk,fi,fr,de,gr,hk,is,in,ie,it,jp,kr,mx,nl,nz,no,pl,pt,za,es,se,ch,tr,gb,us"


@dataclass
class Settings:
    trakt_client_id: str = ""
    trakt_client_secret: str = ""
    trakt_access_token: str = ""
    trakt_refresh_token: str = ""
    trakt_token_expires_at: int = 0  # unix timestamp; 0 = unknown/never obtained via OAuth
    timezone: str = "Europe/Athens"
    endpoint: str = "shows/new"
    genres: str = DEFAULT_GENRES
    countries: str = DEFAULT_COUNTRIES
    network_filter: list[str] = field(default_factory=list)
    pagination_limit: int = 300
    hide_not_watching: bool = False
    cache_ttl_minutes: int = 720  # detail/cast/episode cache lifetime (phase 2)
    day_packing: str = "stacked"   # "stacked" | "packed"
    card_style: str = "vertical"   # "vertical" | "horizontal" | "poster" (poster-only wall, info on hover)
    # Sonarr / Radarr integration (add-to-library buttons)
    sonarr_url: str = ""
    sonarr_api_key: str = ""
    sonarr_quality_profile_id: int = 0
    sonarr_root_folder: str = ""
    sonarr_language_profile_id: int = 1
    radarr_url: str = ""
    radarr_api_key: str = ""
    radarr_quality_profile_id: int = 0
    radarr_root_folder: str = ""
    radarr_minimum_availability: str = "released"
    # Seerr (Overseerr/Jellyseerr lineage) request integration
    seer_url: str = ""
    seer_api_key: str = ""
    # Distrakt (hidden tracker) — network -> Discord emoji map + fallback (§6).
    network_emojis: dict[str, str] = field(default_factory=dict)
    default_network_emoji: str = ":tv:"
    # TMDB API key — used to fetch network logos (distrakt) for Discord emoji art.
    tmdb_api_key: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        clean = {k: v for k, v in (data or {}).items() if k in known}
        # network_filter may arrive as a comma string from the form
        nf = clean.get("network_filter")
        if isinstance(nf, str):
            clean["network_filter"] = [s.strip() for s in nf.split(",") if s.strip()]
        for int_field in ("pagination_limit", "cache_ttl_minutes", "sonarr_quality_profile_id",
                          "sonarr_language_profile_id", "radarr_quality_profile_id",
                          "trakt_token_expires_at"):
            if int_field in clean:
                try:
                    clean[int_field] = int(clean[int_field])
                except (TypeError, ValueError):
                    clean.pop(int_field)
        if "hide_not_watching" in clean:
            clean["hide_not_watching"] = _as_bool(clean["hide_not_watching"])
        # network_emojis may arrive as a JSON string from the emoji-map editor form.
        ne = clean.get("network_emojis")
        if isinstance(ne, str):
            try:
                clean["network_emojis"] = json.loads(ne) if ne.strip() else {}
            except (json.JSONDecodeError, ValueError):
                clean.pop("network_emojis")
        if "network_emojis" in clean and not isinstance(clean["network_emojis"], dict):
            clean.pop("network_emojis")
        return cls(**clean)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def configured(self) -> bool:
        """True once the Trakt credentials have been filled in."""
        return bool(self.trakt_client_id.strip() and self.trakt_access_token.strip())

    @property
    def sonarr_configured(self) -> bool:
        return bool(self.sonarr_url.strip() and self.sonarr_api_key.strip())

    @property
    def radarr_configured(self) -> bool:
        return bool(self.radarr_url.strip() and self.radarr_api_key.strip())

    @property
    def seer_configured(self) -> bool:
        return bool(self.seer_url.strip() and self.seer_api_key.strip())

    @property
    def tmdb_configured(self) -> bool:
        return bool(self.tmdb_api_key.strip())


def _as_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    _ensure_data_dir()
    if SETTINGS_FILE.exists():
        try:
            return Settings.from_dict(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    # First-run: seed from legacy config.php values via env, if provided.
    return Settings(
        trakt_client_id=os.environ.get("TRAKT_CLIENT_ID", ""),
        trakt_access_token=os.environ.get("TRAKT_ACCESS_TOKEN", ""),
    )


def save_settings(settings: Settings) -> None:
    _ensure_data_dir()
    SETTINGS_FILE.write_text(json.dumps(settings.to_dict(), indent=2), encoding="utf-8")
