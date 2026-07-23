"""Front-end-editable configuration (requirement C).

Everything that used to be a hardcoded PHP variable is editable from the Settings
modal in the UI. Persistence is split across three homes, assembled back into one
Settings object by load_settings(): the credentials live in the app_secrets table
(where they can be encrypted at rest), the non-secret globals in app_settings, and
only the two file-only recovery settings (cookie_secure, allow_open_registration)
stay in data/settings.json (git-ignored) so an operator can hand-edit them to
recover from a lockout with no app or database tooling.
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

# NO default genre/country filter. The original PHP script shipped one operator's
# taste as the default — nine excluded genres and a 35-country allowlist — which
# silently removed shows a new install had never been asked about, and read as
# the calendar simply not carrying them. An empty spec filters nothing; the
# Filters panel is where anyone who wants a filter says so.
DEFAULT_GENRES = ""
DEFAULT_COUNTRIES = ""

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

# The only two settings that stay in settings.json. File-only on purpose: an
# operator locked out of the UI can edit a plain file and restart to recover, with
# no app running and no sqlite tooling — and cookie_secure is read on the cookie
# path before the DB is necessarily consulted. Everything else lives in the DB
# (secrets in app_secrets, the rest in app_settings); these two do not move.
RECOVERY_FIELDS = frozenset({"cookie_secure", "allow_open_registration"})


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
    # Calendar window cache lifetime. Trakt's calendar endpoints carry no ETag or
    # Last-Modified (verified against the live API), so a window is refreshed on a
    # short TTL rather than a conditional request. Short because premiere dates in
    # the current/near month shift; a far-past or far-future month rarely changes
    # but costs nothing to leave on the same clock.
    calendar_cache_ttl_minutes: int = 10
    # Total budget for the shared api_cache blob table. The heartbeat evicts the
    # least-recently-stored entries once the summed byte_size crosses this. Detail
    # lookups were measured at ~213 KB each, so 1 GB is on the order of a few
    # thousand of them — a handful of active users' browsing.
    api_cache_max_bytes: int = 1024 * 1024 * 1024
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
    # The distrakt network -> emoji map USED to live here, app-wide. It is per-user
    # now (distrakt_prefs, migration 9): it renders into one person's Discord
    # posts, and sharing it meant any user's roster import registered networks
    # into the operator's map. Nothing seeds a new user's map — it fills in from
    # their own roster, and travels via the tracker's Backup export.
    # TMDB API key — used to fetch network logos (distrakt) for Discord emoji art.
    tmdb_api_key: str = ""
    # Session cookie Secure flag: "always" (default) | "never" | "auto".
    # Deliberately not scheme detection by default — behind a TLS-terminating
    # proxy the app itself is served over plain HTTP, so detection would report
    # "http" and ship session cookies without Secure on every real HTTPS
    # deployment. Use "never" only when genuinely serving over plain HTTP.
    # Editable in Settings > Server; the route guards the one self-locking change
    # (setting "always" from a browser that is genuinely on http). See
    # main._cookie_secure_error.
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
        for int_field in ("pagination_limit", "cache_ttl_minutes", "calendar_cache_ttl_minutes",
                          "api_cache_max_bytes", "sonarr_quality_profile_id",
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
        return cls(**clean)

    def to_dict(self) -> dict:
        """Every field including the secrets — the full internal view. save_settings
        routes each field to its store from here (secrets to app_secrets, globals to
        app_settings, the two recovery fields to settings.json); apply_update merges
        onto it. Anything heading for a client goes through redacted() instead."""
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


def global_field_names() -> frozenset[str]:
    """Every Settings field that persists to the app_settings store — that is, all
    of them except the sealed SECRET_FIELDS and the two file-only RECOVERY_FIELDS.
    Derived from the dataclass so a newly added non-secret field lands there
    automatically."""
    return frozenset(
        name for name in Settings.__dataclass_fields__  # type: ignore[attr-defined]
        if name not in SECRET_FIELDS and name not in RECOVERY_FIELDS
    )


def _read_settings_file() -> dict:
    """The raw settings.json as a dict, or {} when it is missing or unreadable.

    A corrupt file reads as empty rather than raising, so a bad hand-edit degrades
    to defaults (which the operator then sees and fixes) instead of failing boot.
    """
    if not SETTINGS_FILE.exists():
        return {}
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_db_config() -> tuple[dict, dict]:
    """The non-secret globals and the secrets held in the DB, as two dicts.

    Returns ({}, {}) if the config tables are not present yet, so a call before the
    schema is migrated falls back to the file/defaults rather than raising. Values
    are returned verbatim; opening any sealed secret happens at the point of use,
    not here.
    """
    from . import db  # local import: db imports this module, so the cycle is broken here
    try:
        conn = db.connection()
        row = conn.execute("SELECT value FROM app_settings WHERE name = 'app'").fetchone()
        secret_rows = conn.execute("SELECT name, value FROM app_secrets").fetchall()
    except db.DatabaseError:
        return {}, {}
    globals_doc: dict = {}
    if row is not None and row["value"]:
        try:
            loaded = json.loads(row["value"])
            if isinstance(loaded, dict):
                globals_doc = loaded
        except json.JSONDecodeError:
            globals_doc = {}
    secrets = {
        r["name"]: r["value"]
        for r in secret_rows
        if r["name"] in SECRET_FIELDS and r["value"] is not None
    }
    return globals_doc, secrets


def _write_settings_file(data: dict) -> None:
    """Write settings.json atomically (temp file + os.replace) so a crash mid-write
    can never leave a truncated recovery file behind."""
    _ensure_data_dir()
    tmp = SETTINGS_FILE.with_name(SETTINGS_FILE.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, SETTINGS_FILE)


def load_settings() -> Settings:
    """Assemble one Settings object from its three homes: the two recovery fields
    from settings.json, the non-secret globals from app_settings, and the secrets
    from app_secrets. Everything downstream still sees a fully-populated Settings;
    only where each field persists has changed.
    """
    _ensure_data_dir()
    file_data = _read_settings_file()
    globals_doc, secrets = _read_db_config()

    if not file_data and not globals_doc and not secrets:
        # Nothing persisted anywhere: first run. Seed from the legacy config.php
        # values via env exactly as before. Not written back here — the first
        # explicit save persists it.
        return Settings(
            trakt_client_id=os.environ.get("TRAKT_CLIENT_ID", ""),
            trakt_access_token=os.environ.get("TRAKT_ACCESS_TOKEN", ""),
        )

    # The DB is the home for globals and secrets; the file contributes the recovery
    # fields. A value present in the file wins, so an operator can still change any
    # setting by hand-editing settings.json and restarting (the recovery path) —
    # and any non-recovery key added that way is folded into the DB and dropped from
    # the file just below, so the file never re-accumulates config.
    merged = {**globals_doc, **secrets, **file_data}
    settings = Settings.from_dict(merged)

    file_has_db_owned_keys = any(key not in RECOVERY_FIELDS for key in file_data)
    if file_has_db_owned_keys:
        from . import db
        # Only reduce the file when we own the write path (not inside a caller's
        # transaction), so the DB copy is committed before the file is shrunk — a
        # rolled-back transaction must never strand a value that has already been
        # removed from the file. If we are inside one, skip it; the next plain
        # load reduces the file safely.
        if not db.connection().in_transaction:
            save_settings(settings)

    return settings


def save_settings(settings: Settings) -> None:
    """Persist to the three homes: the globals JSON to app_settings, each set
    secret to app_secrets (an unset one is deleted, not stored empty), and the two
    recovery fields to settings.json. The Settings dataclass API is unchanged; only
    the destinations moved.
    """
    _ensure_data_dir()
    from . import db

    data = settings.to_dict()
    globals_doc = {name: data[name] for name in sorted(global_field_names())}
    secrets = {name: data.get(name, "") for name in SECRET_FIELDS}
    recovery = {name: data[name] for name in sorted(RECOVERY_FIELDS)}

    def _persist(conn) -> None:
        conn.execute(
            "INSERT INTO app_settings (name, value) VALUES ('app', ?) "
            "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
            (json.dumps(globals_doc),),
        )
        for name, value in secrets.items():
            if value:
                conn.execute(
                    "INSERT INTO app_secrets (name, value) VALUES (?, ?) "
                    "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
                    (name, value),
                )
            else:
                # An unset secret is absence of a row, so `secrets_set` and storage
                # agree and a cleared credential leaves nothing behind.
                conn.execute("DELETE FROM app_secrets WHERE name = ?", (name,))

    conn = db.connection()
    owns = not conn.in_transaction
    if owns:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _persist(conn)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    else:
        _persist(conn)

    # File last, and only once the DB write is durable: the DB is the source of
    # truth for globals and secrets, so a crash between the two must leave the DB
    # correct and merely the tiny recovery file stale — never a secret stranded in
    # a rolled-back table. When participating in a caller's transaction we cannot
    # know it will commit, so the caller owns the file write in that case.
    if owns:
        _write_settings_file(recovery)
