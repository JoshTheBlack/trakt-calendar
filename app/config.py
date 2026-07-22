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
from urllib.parse import urlsplit

DATA_DIR = Path(os.environ.get("TRAKT_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
SETTINGS_FILE = DATA_DIR / "settings.json"

# Seed for the admin-editable `trusted_proxy_ips` setting below. Hypercorn reads
# the same env var for --forwarded-allow-ips at process start and cannot be
# reconfigured from a running app, which is why this half stays an env var while
# the app's own copy is editable in Settings.
TRUSTED_PROXY_IPS_DEFAULT = "127.0.0.1/32"


def _seed_trusted_proxy_ips() -> str:
    return os.environ.get("TRUSTED_PROXY_IPS", TRUSTED_PROXY_IPS_DEFAULT).strip() or TRUSTED_PROXY_IPS_DEFAULT

# Default genre/country filters carried over from the original PHP script.
DEFAULT_GENRES = "-animation,-anime,-children,-game-show,-home-and-garden,-music,-reality,-special-interest,-talk-show"
DEFAULT_COUNTRIES = "ar,au,at,be,br,ca,cl,cn,co,cz,dk,fi,fr,de,gr,hk,is,in,ie,it,jp,kr,mx,nl,nz,no,pl,pt,za,es,se,ch,tr,gb,us"

# Credentials. These are WRITE-ONLY over the API: they are never sent back to a
# client, only a flag saying whether each one has a value. Everything here is
# either a bearer token or a key that grants access to somebody's account, so a
# route that returned them would hand the whole instance to whoever asked.
#
# `trakt_client_id` is deliberately NOT in this set: it is a public OAuth client
# identifier that ends up in the browser during authorization anyway, and the
# Settings screen needs to show it.
#
# ADDING A SETTING? If it holds a secret, add it here. A test fails on any
# string field whose NAME looks like a credential but is missing from this set.
SECRET_FIELDS = frozenset({
    "trakt_client_secret",
    "trakt_access_token",
    "trakt_refresh_token",
    "sonarr_api_key",
    "radarr_api_key",
    "seer_api_key",
    "tmdb_api_key",
})


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
    # Session cookie Secure flag: "always" (default) | "never" | "auto".
    # Deliberately not scheme detection by default — behind a TLS-terminating
    # proxy the app itself is served over plain HTTP, so detection would report
    # "http" and ship session cookies without Secure on every real HTTPS
    # deployment. Use "never" only when genuinely serving over plain HTTP.
    cookie_secure: str = "always"
    # Comma-separated CIDRs whose X-Forwarded-For this app will honor. Seeded
    # from the TRUSTED_PROXY_IPS env var on first run, editable in Settings after
    # — the correct value depends on the operator's container network and changes
    # when they restructure it.
    trusted_proxy_ips: str = field(default_factory=_seed_trusted_proxy_ips)
    # Off by default: without an invite, registration requires nothing but
    # controlling some Plex or Trakt account (or picking a username), so an
    # open instance would sit open to anyone on the internet. An operator who
    # wants that trades it on deliberately.
    allow_open_registration: bool = False
    # The origin browsers reach this instance on, e.g. https://shows.example.com.
    # EVERY absolute URL the app generates is built from this and never from the
    # Host header, which makes the app structurally immune to Host-header
    # injection and removes any dependency on proxy configuration for URL
    # correctness. Trakt additionally requires the redirect_uri sent in a code
    # exchange to match the one registered on the API application byte for byte,
    # so a value derived from request headers would break the moment anything in
    # front of the app rewrote them. Validated by public_base_url_error().
    public_base_url: str = ""

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
        if "allow_open_registration" in clean:
            clean["allow_open_registration"] = _as_bool(clean["allow_open_registration"])
        # Normalized on the way in as well as validated on save, so a
        # hand-edited settings.json with a trailing slash still builds a correct
        # redirect URI instead of one with a doubled separator in it.
        if isinstance(clean.get("public_base_url"), str):
            clean["public_base_url"] = clean["public_base_url"].strip().rstrip("/")
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
        """Every field including the secrets. For persistence only — anything
        heading for a client goes through redacted()."""
        return asdict(self)

    def redacted(self) -> dict:
        """The settings as an API response: every non-secret field, plus a
        `secrets_set` map saying which credentials currently have a value.

        That map is what lets the Settings screen show "saved" next to a field it
        can no longer read back, and what makes an empty input mean "leave it
        alone" rather than "wipe it".
        """
        data = {k: v for k, v in self.to_dict().items() if k not in SECRET_FIELDS}
        data["secrets_set"] = {
            name: bool(str(getattr(self, name, "") or "").strip())
            for name in sorted(SECRET_FIELDS)
        }
        return data

    @property
    def configured(self) -> bool:
        """True once the Trakt credentials have been filled in."""
        return bool(self.trakt_client_id.strip() and self.trakt_access_token.strip())

    @property
    def trakt_login_configured(self) -> bool:
        """Whether "Log in with Trakt" can be offered.

        All three parts are mandatory and none has a fallback: the client id and
        secret are what the code exchange is authenticated with, and without a
        base URL there is no redirect URI to send — and guessing one from the
        request would produce a value that cannot match what the operator
        registered with Trakt.
        """
        return bool(
            self.trakt_client_id.strip()
            and self.trakt_client_secret.strip()
            and self.public_base_url.strip()
        )

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


def public_base_url_error(value: str) -> str | None:
    """None when `value` is a usable public base URL, otherwise why not.

    Deliberately strict — an absolute http(s) origin and nothing else. A path
    component or a trailing slash would produce a redirect URI that no longer
    matches the one registered on the Trakt API application, and Trakt compares
    the two byte for byte, so a value that merely looks right fails at the code
    exchange with an error that is very hard to read backwards.
    """
    candidate = (value or "").strip()
    if not candidate:
        return None  # absent is allowed; it just leaves provider login off
    parts = urlsplit(candidate)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return "Enter a full URL including http:// or https://."
    if parts.path or parts.query or parts.fragment:
        return "Enter the origin only — no path, query, or trailing slash."
    return None


def apply_update(current: Settings, update: dict) -> Settings:
    """Merge a partial update onto the current settings.

    Ordinary fields overwrite; absent ones are left alone, so a screen that only
    knows about some of the settings can save without clobbering the rest.

    Secrets follow a different rule, because the client can no longer read them
    back and so cannot echo them:

      omitted, or an empty/blank string  -> keep whatever is stored
      a non-empty string                 -> replace it
      an explicit null                   -> clear it

    Without the blank-keeps rule, the first save from a Settings screen whose
    credential inputs render empty would silently wipe every credential the
    instance has.
    """
    data = current.to_dict()
    for key, value in (update or {}).items():
        if key in SECRET_FIELDS:
            if value is None:
                data[key] = ""
            elif isinstance(value, str) and value.strip():
                data[key] = value.strip()
            continue
        data[key] = value
    return Settings.from_dict(data)


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
