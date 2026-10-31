"""Front-end-editable configuration.

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
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import anyio.to_thread

from . import route_params, secrets_box
from .perftrace import span

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("TRAKT_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
SETTINGS_FILE = DATA_DIR / "settings.json"

# The way back into an instance nobody can sign in to. The login page refuses a
# provider sign-in without a base URL, and a base URL that does not match where
# the operator is actually browsing makes the origin check refuse every mutating
# request — including the username/password sign-in — so the only screen that
# could correct it sits behind the setting that is breaking it. Without an
# out-of-band lever the operator's sole option is to hand-edit
# data/settings.json, which is a far riskier thing to reach for. An environment
# variable is the shape used here because it needs no running app and no
# database tooling, matching TRUSTED_PROXY_IPS below.
#
# THIS OVERRIDES A STORED VALUE, it does not merely fill in for a missing one.
# The lockout an operator actually hits is a WRONG stored value, not an absent
# one — an absent one still admits a password sign-in, because the origin check
# falls back to the request's own Host. A lever that only worked when nothing was
# stored could not rescue the case it exists for.
#
# It is still never written to the database on its own: it is applied to the
# assembled Settings after everything is persisted, so unsetting the variable
# removes the override again. Because it wins silently it must ANNOUNCE itself —
# see warn_if_public_base_url_overridden() below and the note the Settings screen
# renders beside the field — or an operator who forgot the variable types a
# correct value into Settings, watches it save, and finds nothing changed.
PUBLIC_BASE_URL_ENV = "PUBLIC_BASE_URL"

# Seed for the admin-editable `trusted_proxy_ips` setting below. Hypercorn reads
# the same env var for --forwarded-allow-ips at process start and cannot be
# reconfigured from a running app, which is why this half stays an env var while
# the app's own copy is editable in Settings.
TRUSTED_PROXY_IPS_DEFAULT = "127.0.0.1/32"


def _seed_trusted_proxy_ips() -> str:
    return os.environ.get("TRUSTED_PROXY_IPS", TRUSTED_PROXY_IPS_DEFAULT).strip() or TRUSTED_PROXY_IPS_DEFAULT

# NO default genre/country/certification filter. The original PHP script shipped
# one operator's taste as the default — nine excluded genres and a 35-country
# allowlist — which silently removed shows a new install had never been asked
# about, and read as the calendar simply not carrying them. An empty spec
# filters nothing; the Filters panel is where anyone who wants a filter says so.
DEFAULT_GENRES = ""
DEFAULT_COUNTRIES = ""
DEFAULT_SHOW_CERTIFICATIONS = ""
DEFAULT_MOVIE_CERTIFICATIONS = ""

# Credentials. These are WRITE-ONLY over the API: they are never sent back to a
# client, only a flag saying whether each one has a value. Everything here is
# either a bearer token or a key that grants access to somebody's account, so a
# route that returned them would hand the whole instance to whoever asked.
#
# `trakt_client_id` and `simkl_client_id` are deliberately NOT in this set: a
# client id is a public OAuth identifier that ends up in the browser during
# authorization anyway, and the Settings screen needs to show it.
#
# ADDING A SETTING? If it holds a secret, add it here. A test fails on any
# string field whose NAME looks like a credential but is missing from this set.
SECRET_FIELDS = frozenset({
    "trakt_client_secret",
    "trakt_access_token",
    "trakt_refresh_token",
    "simkl_client_secret",
    "simkl_access_token",
    "sonarr_api_key",
    "radarr_api_key",
    "seer_api_key",
    "tmdb_api_key",
})

# STORED SECRETS THAT MUST NEVER REACH AN INSTANCE-WIDE Settings OBJECT.
# `simkl_access_token` was once an operator-pasted AUTH V1 token. Under AUTH V2
# the instance holds no Simkl token of its own — only a signed-in person's own
# grant is ever sent, swapped in per request by auth.viewer
# .settings_for_user — so a value stored before the move would otherwise ride
# along on every load_settings() call, make `simkl_configured` read true for a
# caller with no viewer, and be sent with the V2 client id it was never issued
# for. It stays in SECRET_FIELDS, so it remains sealed and redacted in storage.
OPERATOR_SECRETS_NOT_LOADED = frozenset({"simkl_access_token"})

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
    simkl_client_id: str = ""
    # NO LONGER AN OPERATOR-PASTED SETTING. The Settings panel offers no field
    # for this any more: under Simkl's AUTH V2, search and /redirect need a real
    # PERSON's own grant, and every other Simkl call this app makes is public,
    # so there is nothing left for an instance-wide token to authenticate. The
    # field itself stays, because it is still how that person's own token
    # travels: auth.viewer.settings_for_user swaps it in per request,
    # exactly as it swaps in Trakt's. A value an operator saved before the move
    # to AUTH V2 is left in storage untouched (nothing here migrates or clears
    # it), and load_settings deliberately leaves it OUT of the instance-wide
    # object — see OPERATOR_SECRETS_NOT_LOADED — so it can never be sent.
    simkl_client_secret: str = ""
    simkl_access_token: str = ""
    # STILL NO simkl_refresh_token AND NO simkl_token_expires_at, even though
    # Simkl's v2 API does issue a refresh token now (where v1 issued none) —
    # this object was never where a Simkl refresh token would live. Trakt's
    # pair above exists for the OPERATOR's own app-wide device-code link, which
    # Simkl has no equivalent of: the instance holds one shared client
    # id/secret and no Simkl token of its own. Every PERSON's own refresh token lives on their
    # linked_identities row (app/auth/simkl_routes.py.access_token_for_user),
    # exactly where Trakt's per-user one already lived beside these two
    # app-wide fields — so adding a Simkl pair here would duplicate a fact this
    # object was never the home for.
    #
    # WHETHER SIMKL CONTRIBUTES TO THIS INSTANCE'S CALENDAR AT ALL. ON BY
    # DEFAULT, which is exactly the behaviour every existing instance already
    # has: Simkl's calendar files sit on a public CDN and need no client id or
    # token, so the fill has always asked them regardless of whether this
    # instance ever touches the three credential fields above (see
    # providers.calendar_sources, which is where this is applied). This is the
    # operator's lever to say no — reasonable on self-hosted software even when
    # the traffic is harmless, because "harmless" is the app's judgement and not
    # necessarily theirs.
    #
    # IT APPLIES WHETHER OR NOT SIMKL IS CONFIGURED. It once applied only to an
    # instance with no Simkl credentials, and that qualifier was very nearly
    # always true and so said nothing: nothing this app does with the calendar
    # needs an instance-level Simkl token, so almost no instance ever has one,
    # while the rare operator who does was left unable to turn Simkl's calendar
    # off at all.
    #
    # THE FIELD NAME KEEPS ITS OLD SPELLING even though the label no longer says
    # "public": it is the key this value is stored under in app_settings, and
    # renaming it would orphan every choice an operator has already made and
    # silently reset it to the default.
    #
    # IT IS NOT A GATE ON THE TRACKER. Reading somebody's Simkl watch history is
    # a separate question answered by their own linked identity; a calendar that
    # needs no credential is not withheld over an unrelated one.
    simkl_public_calendar_enabled: bool = True
    timezone: str = "Europe/Athens"
    endpoint: str = "shows/new"
    genres: str = DEFAULT_GENRES
    countries: str = DEFAULT_COUNTRIES
    # The instance-wide certification floor, split by media type because shows
    # and movies use different rating vocabularies (TV Parental Guidelines vs.
    # the MPA film ratings) — see app/calendar/filter.py.
    show_certifications: str = DEFAULT_SHOW_CERTIFICATIONS
    movie_certifications: str = DEFAULT_MOVIE_CERTIFICATIONS
    network_filter: list[str] = field(default_factory=list)
    # THE FLOOR'S OTHER SIX DIMENSIONS, each the operator's answer for every
    # viewer and each a viewer filter too (app/calendar/vocab.py's FLOOR_FIELDS
    # says which medium each governs and why). Language and keywords are one
    # floor for both media, like genres and countries; keywords are a LIST,
    # because a TMDB keyword's own name can hold a comma. The rest carry the
    # viewer's column names, so the Settings panel draws them with the Filters
    # panel's own chips. Nothing is filtered by any of them until set.
    languages: str = ""
    keywords: list[str] = field(default_factory=list)
    tv_show_types: str = ""
    tv_single_service: bool = False
    movie_single_service: bool = False
    tv_sparse_no_network: bool = False
    tv_missing: str = ""
    movie_missing: str = ""
    pagination_limit: int = 300
    hide_not_watching: bool = False
    cache_ttl_minutes: int = 720  # detail/cast/episode cache lifetime
    # THE FLOOR ON HOW STALE A STORED SPAN MAY BE, not the whole answer. Trakt's
    # calendar endpoints carry no ETag or Last-Modified (verified against the live
    # API), so a Trakt span is refreshed on a clock rather than revalidated; Simkl's
    # CDN archives do carry validators and are confirmed with a conditional GET,
    # which costs a request but no body.
    #
    # IT IS A FLOOR BECAUSE THE SPAN'S OWN AGE EXTENDS IT — see cache._STALENESS_TIERS,
    # which is where the tiering and the measurements behind it live. This value
    # governs the month a viewer is actually in, and an operator asking for ten
    # minutes still gets ten minutes there. What it no longer does is put a frozen
    # archive from last year on the same clock as tonight's premieres.
    calendar_cache_ttl_minutes: int = 10
    # Total budget for the shared api_cache blob table. The heartbeat evicts the
    # least-recently-stored entries once the summed byte_size crosses this. Detail
    # lookups were measured at ~213 KB each, so 1 GB is on the order of a few
    # thousand of them — a handful of active users' browsing.
    api_cache_max_bytes: int = 1024 * 1024 * 1024
    # Budget for the ranker's poster tile cache (DATA_DIR/posters/), swept LRU by
    # file mtime alongside api_cache_max_bytes. Generous on purpose: at ~95 KB
    # per poster (measured), 10 GB holds on the order of 100,000 of them against
    # a realistic need of a few thousand for a busy instance — this exists so a
    # runaway can't fill the volume, not because anyone should ever think about
    # it. The cache is global and shared across every user on the instance.
    poster_cache_max_bytes: int = 10 * 1024 * 1024 * 1024
    # Budget for the rendered link-preview cards a share link unfurls into
    # (DATA_DIR/share_cards/), swept on the same heartbeat as the two above. Age
    # is the rule that actually fires there — a card is dropped 90 days after it
    # was last written — and this is the backstop that stops a runaway filling
    # the volume in the meantime. At the measured ~160 KB a card, 2 GB is on the
    # order of ten thousand of them, which no single instance approaches inside
    # the retention window.
    share_card_cache_max_bytes: int = 2 * 1024 * 1024 * 1024
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
    # TMDB's API READ ACCESS TOKEN — the long one, sent as a bearer header. A
    # catalogue every surface reads (a title's description, its season totals,
    # its videos, network logos and posters), so it is an instance credential
    # like Trakt's client id rather than anybody's account.
    #
    # THE FIELD KEEPS ITS OLD NAME, `tmdb_api_key`, because that is the key it is
    # sealed under in app_secrets: renaming it would orphan every instance's
    # stored credential. What it HOLDS is now only the read token — see
    # `tmdb_token_error`, which refuses the short v3 key on save, and
    # `tmdb_configured`, which reads a v3 key stored before that refusal existed
    # as no credential at all rather than sending it.
    tmdb_api_key: str = ""
    # THE LANGUAGE TMDB ANSWERS IN, an IETF tag TMDB accepts ("en-US", "de-DE").
    # Sent on every catalogue read and part of every cached answer's key
    # (providers/tmdb/transport.url_for), so changing it never serves an answer
    # written in the old language. One instance setting, deliberately: catalogue
    # answers are cached for everybody, and a per-viewer language would be a
    # per-viewer value reaching shared storage.
    tmdb_language: str = "en-US"
    # THE COUNTRY WHOSE RELEASES THE FILM CALENDAR LISTS, as an ISO 3166-1 code.
    # A film is released on different days in different countries; TMDB's
    # release calendar is asked for one, and this is which.
    tmdb_region: str = "US"
    # Session cookie Secure flag: "always" (default) | "never" | "auto".
    # Deliberately not scheme detection by default — behind a TLS-terminating
    # proxy the app itself is served over plain HTTP, so detection would report
    # "http" and ship session cookies without Secure on every real HTTPS
    # deployment. Use "never" only when genuinely serving over plain HTTP.
    # Editable in Settings > Server; the route guards the one self-locking change
    # (setting "always" from a browser that is genuinely on http). See
    # settings_routes._cookie_secure_error.
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
    # Whether a newly registered account is calendar-approved on arrival, instead
    # of waiting for an administrator.
    #
    # DELIBERATELY INDEPENDENT of allow_open_registration rather than implied by
    # it. Those are two different decisions — who may create an account, and who
    # may see the data — and collapsing them means opening sign-ups silently
    # opens the calendar to anyone who can reach the instance. Keeping them apart
    # also allows the useful middle setting: open sign-up with a human still
    # reviewing each account.
    #
    # Applies to every registration path, so it is also the way to make invites
    # approve on accept regardless of what a particular invite granted. NOT
    # extended to distrakt or the ranker: those stay manual grants.
    auto_approve_calendar: bool = False
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
        """Build Settings from a dict that may have come from a form, a JSON
        file, or the database — so every value may be a string.

        The int and bool coercions are DERIVED FROM THE FIELD'S OWN DECLARED
        TYPE rather than from a list of names kept alongside it. Both lists used
        to be maintained by hand, which meant every new numeric or boolean
        setting needed an edit here as well as above, and a forgotten one showed
        up as a setting that saved fine and then compared wrong (`"0"` is
        truthy). Adding a field is now a one-line change again.
        """
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        clean = {k: v for k, v in (data or {}).items() if k in known}
        # network_filter may arrive as a comma string from the form
        nf = clean.get("network_filter")
        if isinstance(nf, str):
            clean["network_filter"] = [s.strip() for s in nf.split(",") if s.strip()]
        # ...and the keyword floor as ONE KEYWORD A LINE: a keyword's own name can
        # hold a comma ("san francisco, california"), so a comma cannot divide
        # them. Lower-cased, as TMDB spells every keyword.
        kw = clean.get("keywords")
        if isinstance(kw, str):
            clean["keywords"] = [" ".join(line.split()).lower()
                                 for line in kw.splitlines() if line.strip()]
        for name, declared in _scalar_field_types().items():
            if name not in clean:
                continue
            if declared is bool:
                clean[name] = _as_bool(clean[name])
            else:
                # An uncoercible number is DROPPED rather than defaulted here, so
                # the dataclass supplies its own default instead of this method
                # inventing a second answer for what a bad value means.
                try:
                    clean[name] = int(clean[name])
                except (TypeError, ValueError):
                    clean.pop(name)
        # Normalized on the way in as well as validated on save, so a
        # hand-edited settings.json with a trailing slash still builds a correct
        # redirect URI instead of one with a doubled separator in it.
        if isinstance(clean.get("public_base_url"), str):
            clean["public_base_url"] = clean["public_base_url"].strip().rstrip("/")
        # THE CLIENT IDS ARE STRIPPED HERE BECAUSE NOTHING ELSE STRIPS THEM.
        # apply_update trims every SECRET_FIELDS value on the way to storage, and
        # these two are deliberately not in that set (see the note beside it):
        # they are not secret. That left them the only credentials an operator
        # pastes that keep whatever whitespace came with the paste — and a
        # trailing newline is what a paste out of a terminal or a password
        # manager carries.
        #
        # WHAT THAT COSTS, WHICH IS WHY IT IS NORMALIZED RATHER THAN VALIDATED:
        # every `*_configured` predicate below asks `.strip()`, so a padded id
        # reads as configured, and the value that then goes ON THE WIRE is the
        # padded one — Simkl takes its client id as a query parameter, so the
        # space is percent-encoded into the URL and the id becomes one Simkl has
        # never issued. It answers 412, which this app correctly treats as an
        # instance-wide refusal and stops calling on for 900 seconds, sign-in and
        # account linking included. An invisible character at the end of a
        # settings field should not be able to do that.
        for field in ("trakt_client_id", "simkl_client_id"):
            if isinstance(clean.get(field), str):
                clean[field] = clean[field].strip()
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
    def trakt_configured(self) -> bool:
        """True once the Trakt credentials have been filled in.

        Named for the service rather than left as a bare `configured`, which
        read as "this instance is set up" while meaning only "Trakt is". The two
        stop being the same statement the moment there is a second source, and
        the calendar route in particular wants the other question — see
        calendar_source_configured.

        THIS IS THE PRIVATE QUESTION and it is deliberately the narrower of the
        pair: it means "this request can read or write somebody's own Trakt
        data". A gate in front of a PUBLIC catalogue read wants
        trakt_catalogue_configured instead, or losing one person's token
        disables data that never depended on it.
        """
        return bool(self.trakt_client_id.strip() and self.trakt_access_token.strip())

    @property
    def trakt_catalogue_configured(self) -> bool:
        """Whether this instance can make PUBLIC Trakt catalogue reads.

        Trakt's public endpoints — a title's summary, its cast, a season's
        episode list, /search — authenticate with the `trakt-api-key` header
        alone, which carries the INSTANCE's client id. Only the per-person reads
        under /sync/ take a bearer. So the answer to "can we look a title up" is
        the client id and nothing else, and asking trakt_configured in front of
        one of those reads asks whether a PARTICULAR VIEWER linked Trakt — which
        made an instance-wide, globally-cached fact hinge on one account's token
        and turned a Simkl-only viewer's every roster row into an error.

        Two questions rather than one widened one: the sync gates genuinely do
        need the token, and quietly making trakt_configured mean less would
        weaken every one of them at once.
        """
        return bool(self.trakt_client_id.strip())

    @property
    def calendar_source_configured(self) -> bool:
        """Whether ANY registered source can supply the calendar.

        This is the question the calendar page, the day fragment and the public
        share pages are actually asking before they try to read a month: not
        "does Trakt work" but "is there anybody to ask". Today the two answers
        coincide because Trakt is the only registered source that publishes a
        calendar; they diverge the moment a second one does, and this is the call
        site that then needs no edit.
        """
        from . import providers  # local: providers -> trakt -> config would cycle
        return bool(providers.for_calendar_sources(self))

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
    def simkl_configured(self) -> bool:
        """True once this instance can ask Simkl a question that needs a token.

        The pair Trakt's property uses, for the same reason: an id names the
        application and a token authorizes it, and neither alone can be sent
        anywhere useful.

        WORTH KNOWING, because it is not true of Trakt: the two halves of Simkl
        this app reads are not equally gated. Its calendar arrives from public
        CDN files that take no client id and no token at all, so a source that
        is "not configured" by this property can still answer for a month. This
        property is about the AUTHENTICATED half — the person's own library —
        which is what the registry asks about before offering the source.
        """
        return bool(self.simkl_client_id.strip() and self.simkl_access_token.strip())

    @property
    def simkl_catalogue_configured(self) -> bool:
        """Whether this instance can make PUBLIC Simkl catalogue reads.

        The pair to `trakt_catalogue_configured`, drawn for the same reason and
        with a bigger gap between the halves: Simkl's per-title endpoints — a
        show's record, its episode list — take the client id as a query parameter
        and no bearer at all (app/providers/simkl/transport.py's `api_params`),
        so the answer to "can we look a title up" is the client id and nothing
        else. Asking `simkl_configured` in front of one of those asks whether
        this instance has issued a Simkl TOKEN, which essentially no instance
        running only the public halves ever has — the author's own live instance
        has a client id and no token — and would refuse every Simkl-only title's
        detail modal on a credential the lookup never sends.

        The calendar is a third answer again and is not this one: those files
        come from a CDN that takes neither (see calendar.py in that package), so
        a source that is unconfigured by BOTH properties still fills a month.
        """
        return bool(self.simkl_client_id.strip())

    @property
    def simkl_login_configured(self) -> bool:
        """Whether "Log in with Simkl" can be offered.

        All three parts are mandatory and none has a fallback: the client id and
        secret authenticate the code exchange, and without a base URL there is no
        redirect URI to send — and one guessed from the request headers could not
        match what the operator registered with Simkl, which compares it byte for
        byte exactly as Trakt does.
        """
        return bool(
            self.simkl_client_id.strip()
            and self.simkl_client_secret.strip()
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
        """Whether this instance holds a TMDB credential it will SEND.

        A READ TOKEN AND NOTHING ELSE. A v3 key stored before the Settings field
        began refusing one is still in app_secrets, and reading it as configured
        would put TMDB first in front of every title and then answer 401 on every
        call. Read as unset, TMDB simply steps aside and the other services
        describe titles as they did; the Settings page says why
        (`tmdb_credential_problem`).
        """
        token = self.tmdb_api_key.strip()
        return bool(token) and tmdb_token_error(token) is None

    @property
    def tmdb_credential_problem(self) -> str:
        """The sentence the Settings page draws under a stored TMDB credential
        this app will not send, or "". Saving cannot have produced one any more;
        an instance that stored a v3 key earlier sees this instead of a TMDB that
        silently does nothing."""
        token = self.tmdb_api_key.strip()
        return (tmdb_token_error(token) or "") if token else ""


# ---------------------------------------------------------------------------
# TMDB's three settings, judged on the way in
# ---------------------------------------------------------------------------

# TMDB'S READ TOKEN IS A JWT: three base64url segments joined by dots, the first
# of which decodes to a JSON header and so always begins "eyJ". The v3 key TMDB
# also issues is 32 hex characters and travels in the query string, which this
# app will not do — a credential in a URL reaches access logs and cache keys.
_JWT = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+")
_TMDB_TOKEN_SENTENCE = (
    "That isn't a TMDB API Read Access Token. Paste the long token from "
    "themoviedb.org → Settings → API (\"API Read Access Token\"), not the short "
    "API key.")
# TMDB's language parameter is an ISO 639-1 language, optionally with an ISO
# 3166-1 country ("en", "en-US"); its region parameter is the country alone.
_TMDB_LANGUAGE = re.compile(r"[a-z]{2}(-[A-Z]{2})?")
_TMDB_REGION = re.compile(r"[A-Z]{2}")


def tmdb_token_error(value: str) -> str | None:
    """The sentence refusing `value` as TMDB's credential, or None when it is
    a read token. An empty value is not this function's concern — an empty
    credential field means "leave it alone" (apply_form)."""
    return None if _JWT.fullmatch(value.strip()) else _TMDB_TOKEN_SENTENCE


def tmdb_language_error(value: str) -> str | None:
    if _TMDB_LANGUAGE.fullmatch(value):
        return None
    return "TMDB language must look like en-US (a language, optionally a country)."


def tmdb_region_error(value: str) -> str | None:
    if _TMDB_REGION.fullmatch(value):
        return None
    return "TMDB region must be a two-letter country code, like US or GB."


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


def public_base_url_override() -> str:
    """The PUBLIC_BASE_URL environment variable, or "" when it is unset or unusable.

    Read live rather than captured at import, so the variable can be added and the
    process restarted without anything else having to know about it.

    Validated with the same rule the Settings screen applies, because a value that
    only looks right produces a redirect URI Trakt refuses byte-for-byte and the
    failure surfaces much later as an unreadable OAuth error. A bad one is logged
    and ignored rather than raised: this exists to rescue an instance nobody can
    sign in to, so it must never be the reason the process will not start — and an
    ignored value leaves the stored one in force, which is the safer of the two.
    """
    candidate = os.environ.get(PUBLIC_BASE_URL_ENV, "").strip().rstrip("/")
    if not candidate:
        return ""
    if error := public_base_url_error(candidate):
        logger.warning("Ignoring %s=%r: %s", PUBLIC_BASE_URL_ENV, candidate, error)
        return ""
    return candidate


async def warn_if_public_base_url_overridden() -> None:
    """Say loudly, once at startup, that the environment is winning over a
    DIFFERENT stored base URL.

    An override nobody can see is a trap: the operator sets the variable to get
    back in, forgets it, later types the real value into Settings, watches it
    save, and finds the app still building links and checking origins against the
    old one. So the line names BOTH values and says what to do about it, rather
    than merely noting that a variable is set.

    Silent when the two agree — that is the ordinary state of an instance whose
    operator pins the value in its environment, and warning about it would train
    everyone to ignore the line that matters.
    """
    override = public_base_url_override()
    if not override:
        return
    stored = (await load_settings(open_secrets=False, apply_env_override=False)).public_base_url
    if stored == override:
        return
    logger.warning(
        "%s=%s is overriding the saved public base URL (%s). Links and the "
        "cross-origin check use the environment value, and a different one saved "
        "in Settings will not take effect until the variable is removed and the "
        "app restarted.",
        PUBLIC_BASE_URL_ENV, override, stored or "unset",
    )


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


# ---------------------------------------------------------------------------
# the Settings form
# ---------------------------------------------------------------------------
#
# THE ONE PLACE A SUBMITTED VALUE BECOMES A SETTING. The Settings panel is markup
# the server draws, and a form sends strings: "300", "true", "on", a byte cap in
# megabytes, an empty password field. What each of those means used to be forty
# lines of parseInt and `=== 'true'` in the browser, each with its own fallback
# restating a default this dataclass already declares — and a wrong coercion
# there wrote a wrong setting whose failure showed up somewhere else, later.
#
# THE TYPE STILL COMES FROM THE FIELD'S DECLARATION (`_scalar_field_types`), so a
# new setting is still a one-line change. What this section adds is the two facts
# a declaration cannot carry — how a number is typed, and what range of it is
# sane — and a refusal for a value outside either, rather than the silent default
# `Settings.from_dict` falls back to for a hand-edited file.

MB = 1024 * 1024
GB = 1024 * MB


@dataclass(frozen=True)
class NumberField:
    """A whole-number setting as the Settings form offers it.

    `unit` is how many stored units one typed unit is. The two cache caps are
    STORED in bytes and TYPED in megabytes and gigabytes, because nobody wants to
    count zeros — and that conversion is a type decision like any other, so it
    lives here rather than beside the input.

    The bounds are the ones the form's own `min`/`max` render from, so the browser
    refuses what this refuses and the two cannot disagree about a limit.
    """
    label: str
    low: int
    high: int
    unit: int = 1


# The id of a quality profile is Sonarr's or Radarr's to issue, so its only sane
# range is "a non-negative database id"; 0 is this app's "none chosen".
_SERVICE_ID = (0, 2**31 - 1)

NUMBER_FIELDS: dict[str, NumberField] = {
    "pagination_limit": NumberField("Pagination limit", 1, 1000),
    "cache_ttl_minutes": NumberField("Detail cache (minutes)", 0, 10080),
    "calendar_cache_ttl_minutes": NumberField("Calendar cache (minutes)", 1, 1440),
    "api_cache_max_bytes": NumberField("Cache size cap (MB)", 16, 65536, MB),
    "poster_cache_max_bytes": NumberField("Poster cache cap (GB)", 1, 1024, GB),
    "sonarr_quality_profile_id": NumberField("Sonarr quality profile", *_SERVICE_ID),
    "radarr_quality_profile_id": NumberField("Radarr quality profile", *_SERVICE_ID),
}

# A stored secret is never drawn back into markup, so its field arrives EMPTY and
# an empty field means "leave it alone". Removing one is therefore a control of
# its own, posted under this prefix — named here, beside the rule that reads it,
# so the panel never spells the format for itself.
CLEAR_PREFIX = "clear:"


def clear_field(name: str) -> str:
    """The form field that asks for the secret `name` to be removed."""
    return f"{CLEAR_PREFIX}{name}"


class SettingsFormError(ValueError):
    """A submitted value the writer will not store. Its message is the sentence
    the Settings panel shows, and nothing has been written when it is raised."""


@dataclass(frozen=True)
class SecretInput:
    """One credential field as the panel draws it: its name, the name its clear
    control posts under, and whether a value is stored. NEVER the value — not
    masked, not its length."""
    name: str
    clear_name: str
    is_set: bool


@dataclass(frozen=True)
class NumberInput:
    """One number field as the panel draws it, already in the unit it is typed in."""
    name: str
    label: str
    low: int
    high: int
    value: int


def secret_inputs(settings: Settings) -> dict[str, SecretInput]:
    """Every credential field's drawable state, from `redacted()`'s own flags so
    "is it set" has one answer."""
    stored = settings.redacted()["secrets_set"]
    return {name: SecretInput(name, clear_field(name), stored[name]) for name in stored}


def number_inputs(settings: Settings) -> dict[str, NumberInput]:
    """Every number field's drawable state, converted into its typed unit."""
    return {
        name: NumberInput(name, spec.label, spec.low, spec.high,
                          round(getattr(settings, name) / spec.unit))
        for name, spec in NUMBER_FIELDS.items()
    }


def _form_number(name: str, value: object) -> int:
    spec = NUMBER_FIELDS.get(name)
    label = spec.label if spec else name.replace("_", " ").capitalize()
    try:
        number = int(str(value).strip())
    except ValueError:
        raise SettingsFormError(f"{label} must be a whole number.") from None
    if spec is None:
        return number
    if not spec.low <= number <= spec.high:
        raise SettingsFormError(f"{label} must be between {spec.low} and {spec.high}.")
    return number * spec.unit


def apply_form(current: Settings, form: dict) -> Settings:
    """The settings a submitted Settings form describes, merged onto `current`.

    Every field is read by what its declaration says it is:

      a credential     a typed value replaces it; otherwise its `clear:` box,
                       ticked, removes it; otherwise it is left exactly as stored
      a bool           a checkbox, a two-option select or a JSON boolean alike
      an int           a whole number inside NUMBER_FIELDS' bounds, converted
                       from the unit it is typed in — anything else is REFUSED
      anything else    the text as typed, trimmed

    A field the form does not mention is left alone, as `apply_update` has always
    done, so a panel that draws some settings cannot clobber the rest.

    Raises SettingsFormError, having changed nothing, for a value it will not
    store. The public base URL is judged on the merged result because
    `Settings.from_dict` normalizes it (a trailing slash) before it is used.
    """
    declared = _scalar_field_types()
    update: dict[str, object] = {}
    for name, spec in Settings.__dataclass_fields__.items():  # type: ignore[attr-defined]
        if name in SECRET_FIELDS:
            typed = route_params.last_value(form.get(name))
            if isinstance(typed, str) and typed.strip():
                if error := _typed_secret_error(name, typed):
                    raise SettingsFormError(error)
                update[name] = typed
            elif route_params.ticked(form.get(clear_field(name))):
                update[name] = None
            continue
        if name not in form:
            continue
        kind = declared.get(name)
        # A list-typed setting (the network floor) is typed as one comma-joined
        # line, which from_dict splits; only a scalar can have arrived twice.
        value = form[name] if spec.type.startswith("list") else route_params.last_value(form[name])
        if kind is bool:
            update[name] = _as_bool(value)
        elif kind is int:
            update[name] = _form_number(name, value)
        elif isinstance(value, str):
            update[name] = value.strip()
        else:
            update[name] = value
    settings = apply_update(current, update)
    for error in (public_base_url_error(settings.public_base_url),
                  tmdb_language_error(settings.tmdb_language),
                  tmdb_region_error(settings.tmdb_region)):
        if error:
            raise SettingsFormError(error)
    return settings


# A credential whose SHAPE says it is the wrong one, refused with a sentence
# rather than stored. Judged only on a value somebody TYPED: a stored credential
# is never drawn back, so a blank field (keep it) and a clear box (remove it)
# have nothing to judge.
_SECRET_SHAPES = {"tmdb_api_key": tmdb_token_error}


def _typed_secret_error(name: str, typed: str) -> str | None:
    judge = _SECRET_SHAPES.get(name)
    return judge(typed) if judge else None


def _as_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return route_params.ticked(v)


@lru_cache(maxsize=1)
def _scalar_field_types() -> dict[str, type]:
    """{field name: bool or int} for the Settings fields declared as one of
    those two. Every other field is absent, which is how from_dict knows to
    leave it alone.

    Matched on the annotation as WRITTEN, because `from __future__ import
    annotations` leaves every one of them a string rather than a type object.
    That also side-steps the trap in comparing them as types at all: bool is a
    subclass of int in Python, so an `issubclass` test would quietly coerce
    every flag through int() and turn it into 0 or 1.
    """
    by_name = {"bool": bool, "int": int}
    return {
        name: by_name[f.type]
        for name, f in Settings.__dataclass_fields__.items()  # type: ignore[attr-defined]
        if f.type in by_name
    }


def ensure_data_dir() -> None:
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


def _read_db_config(conn) -> tuple[dict, dict]:
    """The non-secret globals and the secrets held in the DB, as two dicts.

    Returns ({}, {}) if the config tables are not present yet, so a call before the
    schema is migrated falls back to the file/defaults rather than raising. Values
    are returned verbatim; opening any sealed secret happens at the point of use,
    not here.
    """
    from . import db  # local import: db imports this module, so the cycle is broken here
    try:
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
    ensure_data_dir()
    tmp = SETTINGS_FILE.with_name(SETTINGS_FILE.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, SETTINGS_FILE)


async def load_settings(open_secrets: bool = True, *, apply_env_override: bool = True) -> Settings:
    """Assemble one Settings object from its three homes: the two recovery fields
    from settings.json, the non-secret globals from app_settings, and the secrets
    from app_secrets. Everything downstream still sees a fully-populated Settings;
    only where each field persists has changed.

    `open_secrets=False` skips decrypting the stored secrets entirely, leaving those
    fields at their empty defaults. The session/cookie and origin checks in the auth
    layer need only the globals and recovery fields, never a credential, so they load
    this way — which also means authentication keeps working when a stored secret is
    sealed under a key the current one cannot open (that decrypt would otherwise raise
    SealedButWrongKey), so an administrator can still reach the recovery screen.

    `apply_env_override=False` reports `public_base_url` as STORED, ignoring the
    PUBLIC_BASE_URL environment variable. Only the startup warning asks for that —
    it is the one caller that needs the value the override is displacing, so that
    the line it logs can name both. Every other caller wants what is in force.

    ON A WORKER THREAD, THROUGH db.run, like every other read in the app: reading
    settings.json, the two queries and decrypting every stored secret used to run
    on whichever thread asked — almost always the event loop — and a writer
    holding the database's lock stalled every request behind a settings read for
    up to the busy timeout. UNCACHED, so a saved setting takes effect on the very
    next request, at one thread hop per call.
    """
    from . import db  # local import: db imports this module, so the cycle is broken here
    with span("config.load_settings", secrets=open_secrets):
        settings, reduce = await db.run(
            lambda conn: _assemble_settings(conn, open_secrets, apply_env_override))
    if reduce:
        # A hand-edited settings.json holding more than the recovery fields is
        # folded into the database and the file shrunk back — by a save of its
        # own, so the database copy is committed before the file is shrunk and a
        # rolled-back write can never strand a value already gone from the file.
        #
        # THIS SAVE MUST NEVER CLEAR A SECRET, which is why it relies on
        # save_settings' default. `settings` here can legitimately carry blank
        # credentials that are not blank in storage: every open_secrets=False
        # caller (the request-shape guard, sign-in, the session lookup) loads
        # exactly that way, and one of them runs on essentially every request.
        # Letting this write treat "blank" as "delete" meant a hand-edited
        # settings.json — the documented recovery path — wiped every stored
        # credential the moment anybody used the app.
        await save_settings(settings)

    # Last, so the override is never what the file reduction above persists: it
    # wins over the stored value in memory and leaves no trace of its own in the
    # database, which is what makes removing the variable enough to undo it.
    if apply_env_override and (override := public_base_url_override()):
        settings.public_base_url = override

    return settings


def _assemble_settings(conn, open_secrets: bool,
                       apply_env_override: bool) -> tuple[Settings, bool]:
    """`load_settings`' reading and merging, on the worker thread holding `conn`:
    (the settings, whether settings.json holds keys the database owns and should
    be shrunk — never while a sealed secret could not be read, `degraded` below)."""
    ensure_data_dir()
    file_data = _read_settings_file()
    globals_doc, stored_secrets = _read_db_config(conn)
    if not open_secrets:
        stored_secrets = {}

    if not file_data and not globals_doc and not stored_secrets:
        # Nothing persisted anywhere: first run. Seed from the legacy config.php
        # values via env exactly as before. Not written back here — the first
        # explicit save persists it.
        return Settings(
            trakt_client_id=os.environ.get("TRAKT_CLIENT_ID", ""),
            trakt_access_token=os.environ.get("TRAKT_ACCESS_TOKEN", ""),
            public_base_url=public_base_url_override() if apply_env_override else "",
        ), False

    # Decrypt the stored secrets here so every downstream reader (trakt.py, arr.py,
    # seer.py, the token refresh in main.py) sees plaintext through the ordinary
    # Settings fields and knows nothing about sealing. A sealed value with no key
    # opens to None: drop it so the field reads as UNSET rather than shipping
    # `enc:v1:...` to a provider — the ciphertext stays intact in the row and comes
    # back when the key is restored. A wrong key raises out of open_() (fail loud),
    # which is deliberately not caught here. `degraded` records that a sealed value
    # could not be read, so the automatic file-reduction below is skipped: rewriting
    # secrets while a real value reads as blank would overwrite it for good.
    secrets: dict[str, str] = {}
    degraded = False
    for name, stored in stored_secrets.items():
        if name in OPERATOR_SECRETS_NOT_LOADED:
            continue
        plain = secrets_box.open_(stored)
        if plain is None:
            if stored.startswith(secrets_box.PREFIX):
                degraded = True
            continue
        secrets[name] = plain

    # The DB is the home for globals and secrets; the file contributes the recovery
    # fields. A value present in the file wins, so an operator can still change any
    # setting by hand-editing settings.json and restarting (the recovery path) —
    # and any non-recovery key added that way is folded into the DB and dropped from
    # the file just below, so the file never re-accumulates config.
    merged = {**globals_doc, **secrets, **file_data}
    # Filtered here as well as in the decrypt loop above, so a hand-edited
    # settings.json cannot put one back either.
    for name in OPERATOR_SECRETS_NOT_LOADED:
        merged.pop(name, None)
    settings = Settings.from_dict(merged)
    # Skipped entirely while `degraded`, because re-saving would seal the
    # blanked-out secrets and destroy the values the restored key could still
    # recover.
    file_has_db_owned_keys = any(key not in RECOVERY_FIELDS for key in file_data)
    return settings, file_has_db_owned_keys and not degraded


async def save_settings(settings: Settings, *, clear_unset_secrets: bool = False) -> None:
    """Persist to the three homes: the globals JSON to app_settings, each set
    secret to app_secrets, and the two recovery fields to settings.json. The
    Settings dataclass API is unchanged; only the destinations moved.

    A BLANK SECRET IS LEFT ALONE UNLESS `clear_unset_secrets` SAYS OTHERWISE.
    A Settings object cannot tell "the operator emptied this field" apart from
    "this value was never loaded" — both read as "" — and the two want opposite
    outcomes. Deleting by default made the second one destructive: any caller
    holding a partially-populated Settings (load_settings(open_secrets=False), or
    one assembled over a hand-edited settings.json) silently removed every
    credential the instance had, with nothing left to restore them from.
    So the deleting behaviour is opt-in, and only a path that has genuinely
    resolved the operator's intent asks for it — settings_routes' save, where
    config.apply_update has already turned "omitted" into keep and an explicit
    null into clear. Deleting a row that is not there is a no-op, so an
    intentional clear still leaves nothing behind.

    THROUGH db.transaction, ON A WORKER THREAD, like every other write: it used to
    take the write lock on whichever thread asked — the event loop — so while
    another writer held the lock every request waited behind it, and the save
    then raised. `write_settings` is the database half, for a caller composing a
    save into a transaction of its own.
    """
    from . import db  # local import: db imports this module, so the cycle is broken here

    recovery = await db.transaction(
        lambda conn: write_settings(conn, settings, clear_unset_secrets=clear_unset_secrets))
    # File last, and only once the DB write is durable: the DB is the source of
    # truth for globals and secrets, so a crash between the two must leave the DB
    # correct and merely the tiny recovery file stale — never a secret stranded in
    # a rolled-back table.
    await anyio.to_thread.run_sync(_write_settings_file, recovery)


def write_settings(conn, settings: Settings, *, clear_unset_secrets: bool = False) -> dict:
    """`save_settings`' database half — the globals and the secrets — inside the
    caller's transaction (`save_settings` says what `clear_unset_secrets` means).
    Returns the recovery fields, for settings.json: the caller writes the file
    once its transaction has committed, since only it knows that it will."""
    from .auth import encryption_flow  # local: encryption_flow -> config would cycle

    data = settings.to_dict()
    globals_doc = {name: data[name] for name in sorted(global_field_names())}
    secrets = {name: data.get(name, "") for name in SECRET_FIELDS}
    recovery = {name: data[name] for name in sorted(RECOVERY_FIELDS)}

    # While the key is missing or wrong, sealing a secret would write plaintext over
    # ciphertext the original key could still recover (no key) or bury an unreadable
    # value under a second key's ciphertext (wrong key). The globals and the recovery
    # file are never sealed, so those still persist; the secret rows are left exactly
    # as they are. Callers that must not silently drop a credential (the admin Save)
    # check this first and refuse loudly, routing to recovery — this is the last-line
    # guard for every other save path.
    write_secrets = not encryption_flow.secret_writes_blocked()

    conn.execute(
        "INSERT INTO app_settings (name, value) VALUES ('app', ?) "
        "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
        (json.dumps(globals_doc),),
    )
    if not write_secrets:
        return recovery
    for name, value in secrets.items():
        if value:
            # Sealed before it lands in the row when a key is configured, and
            # stored verbatim when not — the plaintext value is what decides
            # store-vs-delete, so a cleared credential still leaves no row.
            conn.execute(
                "INSERT INTO app_secrets (name, value) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
                (name, secrets_box.seal(value)),
            )
        elif clear_unset_secrets:
            # An unset secret is absence of a row, so `secrets_set` and storage
            # agree and a cleared credential leaves nothing behind.
            conn.execute("DELETE FROM app_secrets WHERE name = ?", (name,))
        # Otherwise the stored row stands: see the docstring — a blank field
        # on this object is not evidence that anybody asked for it to go.
    return recovery
