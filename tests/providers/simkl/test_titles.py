"""Simkl's per-title CATALOG detail (GET /tv/{id}, GET /movies/{id}) — the
enrichment drain's only source of genres, network, country, certification,
runtime, status and overview.

Guards the two measured, non-obvious facts titles.py's docstring documents:
GET /tv/{id} answers for an anime id too (so there is no third endpoint), and
Simkl's "not found" does not reliably come back as a 404 (so a caller cannot
trust the status code alone).
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app.providers.base import Media
from app.providers.simkl import titles, transport

SETTINGS = SimpleNamespace(simkl_client_id="cid", simkl_access_token="", cache_ttl_minutes=10)

ANIME_PAYLOAD = {
    "title": "One Piece",
    "ids": {"simkl": 38636, "slug": "one-piece", "tmdb": "37854", "imdb": "tt0388629",
            "tvdb": "81797", "mal": "21", "anidb": "69"},
    "genres": ["Action", "Adventure", "Martial Arts"],
    "country": "JP",
    "certification": "PG-13",
    "network": "Fuji TV",
    "status": "airing",
    "runtime": 24,
    "overview": "A pirate crew searches for treasure.",
    "type": "anime",
}

TV_PAYLOAD = {
    "title": "Breaking Bad",
    "ids": {"simkl": 11121, "slug": "breaking-bad", "tmdb": "1396", "imdb": "tt0903747",
            "tvdb": "81189"},
    "genres": ["Crime", "Drama"],
    "country": "US",
    "certification": "TV-MA",
    "network": "AMC",
    "status": "ended",
    "runtime": 47,
    "overview": "A teacher turns to crime.",
}


def _cached_get(*answers):
    return AsyncMock(side_effect=list(answers))


class FetchTitleTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_show_is_read_through_the_tv_path(self):
        spy = _cached_get(TV_PAYLOAD)
        with patch("app.providers.simkl.transport.cached_get", new=spy):
            fields = await titles.fetch_title(SETTINGS, 11121, Media.SHOW)
        call = spy.await_args
        self.assertEqual(call.args[2], "tv/11121")
        self.assertIs(call.kwargs["pool"], transport.CATALOG_POOL)
        self.assertEqual(fields["genres"], ["crime", "drama"])
        self.assertEqual(fields["country"], "US")
        self.assertEqual(fields["certification"], "TV-MA")
        self.assertEqual(fields["network"], "AMC")
        self.assertEqual(fields["status"], "ended")
        self.assertEqual(fields["runtime"], 47)
        # THE WHOLE ids MAP, NOT AN ALLOWLIST — every namespace the payload
        # named survives, coerced to str, including ones the calendar file
        # already supplied (simkl, slug, tmdb, imdb): completeness beats
        # selectivity here because ids are small and bounded.
        self.assertEqual(fields["ids"], {
            "simkl": "11121", "slug": "breaking-bad", "tmdb": "1396",
            "imdb": "tt0903747", "tvdb": "81189",
        })
        self.assertEqual(fields["extract_version"], titles.EXTRACT_VERSION)

    async def test_a_movie_is_read_through_the_movies_path(self):
        payload = {**TV_PAYLOAD, "ids": {"simkl": 472214, "tmdb": "27205"}}
        spy = _cached_get(payload)
        with patch("app.providers.simkl.transport.cached_get", new=spy):
            await titles.fetch_title(SETTINGS, 472214, Media.MOVIE)
        self.assertEqual(spy.await_args.args[2], "movies/472214")

    async def test_an_anime_id_answers_through_the_tv_path_with_no_second_call(self):
        """Measured live 2026-08-06: GET /tv/{id} for an anime id returns every
        field GET /anime/{id} does. A Simkl 'show' Record never records whether
        the title is TV or anime, so this is the only way enrichment can work
        without guessing — and it must cost exactly one call, not a fallback."""
        spy = _cached_get(ANIME_PAYLOAD)
        with patch("app.providers.simkl.transport.cached_get", new=spy):
            fields = await titles.fetch_title(SETTINGS, 38636, Media.SHOW)
        self.assertEqual(spy.await_args.args[2], "tv/38636")
        spy.assert_awaited_once()
        # "anime" is appended from the payload's own stated "type", not
        # inferred from having followed a redirect to get here.
        self.assertEqual(fields["genres"],
                         ["action", "adventure", "martial-arts", "anime"])
        self.assertEqual(fields["ids"], {
            "simkl": "38636", "slug": "one-piece", "tmdb": "37854",
            "imdb": "tt0388629", "tvdb": "81797", "mal": "21", "anidb": "69",
        })
        self.assertEqual(fields["type"], "anime")

    async def test_an_unusual_rare_id_namespace_survives_extraction(self):
        """Measured live 2026-08-06: `tmdbtv` and `trakttvslug` each appeared
        on exactly one title across 300 sampled — precisely the kind of
        namespace an allowlist would have silently dropped and nobody would
        have noticed missing."""
        payload = {**TV_PAYLOAD, "ids": {**TV_PAYLOAD["ids"], "tmdbtv": "999888"}}
        with patch("app.providers.simkl.transport.cached_get", new=_cached_get(payload)):
            fields = await titles.fetch_title(SETTINGS, 1, Media.SHOW)
        self.assertEqual(fields["ids"]["tmdbtv"], "999888")

    async def test_relations_and_users_recommendations_are_not_stored(self):
        """These two are measured to run tens of kilobytes each and nothing
        in this app reads them — the whitelist discipline for the REST of the
        payload (everything but `ids`) still applies."""
        payload = {**TV_PAYLOAD, "relations": [{"title": "x"} for _ in range(500)],
                  "users_recommendations": [{"title": "y"} for _ in range(500)]}
        with patch("app.providers.simkl.transport.cached_get", new=_cached_get(payload)):
            fields = await titles.fetch_title(SETTINGS, 1, Media.SHOW)
        self.assertNotIn("relations", fields)
        self.assertNotIn("users_recommendations", fields)

    async def test_anime_type_and_trailers_are_captured(self):
        payload = {**TV_PAYLOAD, "type": "anime", "anime_type": "movie",
                  "total_episodes": 1, "poster": "abc123",
                  "first_aired": "2026-01-01T00:00:00Z",
                  "trailers": [{"name": "Trailer", "youtube": "abc123"}]}
        with patch("app.providers.simkl.transport.cached_get", new=_cached_get(payload)):
            fields = await titles.fetch_title(SETTINGS, 1, Media.SHOW)
        self.assertEqual(fields["anime_type"], "movie")
        self.assertEqual(fields["total_episodes"], 1)
        self.assertEqual(fields["poster"], "abc123")
        self.assertEqual(fields["first_aired"], "2026-01-01T00:00:00Z")
        self.assertEqual(fields["trailers"], [{"name": "Trailer", "youtube": "abc123"}])

    async def test_a_serial_anime_type_is_captured_as_is_not_pruned_here(self):
        """This module only records the value; app/calendar/filter.py decides
        what to do with it. ona/ova/tv/special are all serial formats and
        must not be confused with `movie` anywhere downstream."""
        for serial_type in ("ona", "ova", "tv", "special"):
            payload = {**TV_PAYLOAD, "anime_type": serial_type}
            with patch("app.providers.simkl.transport.cached_get", new=_cached_get(payload)):
                fields = await titles.fetch_title(SETTINGS, 1, Media.SHOW)
            self.assertEqual(fields["anime_type"], serial_type)

    async def test_genres_are_slugged_to_match_trakts_own_spelling(self):
        """Simkl spells 'Game Show'; Trakt's calendar payload already arrives as
        'game-show', which is what a viewer's filter spec is written against."""
        payload = {**TV_PAYLOAD, "genres": ["Game Show", "Reality"]}
        with patch("app.providers.simkl.transport.cached_get", new=_cached_get(payload)):
            fields = await titles.fetch_title(SETTINGS, 1, Media.SHOW)
        self.assertEqual(fields["genres"], ["game-show", "reality"])

    async def test_an_empty_list_answer_is_treated_as_not_found(self):
        """Measured live: an id Simkl does not recognise answers 200 with `[]`,
        never a 404 — the shape /tv/episodes/{id} already uses for the same
        reason (see test_detail.py)."""
        with patch("app.providers.simkl.transport.cached_get", new=_cached_get([])):
            self.assertIsNone(await titles.fetch_title(SETTINGS, 999999999, Media.SHOW))

    async def test_a_page_that_is_not_a_title_is_treated_as_not_found(self):
        """Measured live: id 0 and a non-numeric tv id both answer 200 with an
        unrelated 'top aired' digest rather than an error or an empty list.
        Accepting anything without `ids`+`title` would enrich a record with
        data belonging to no title at all."""
        garbage = {"top_aired_fanarts": [{"title": "All Elite Wrestling: Dynamite"}]}
        with patch("app.providers.simkl.transport.cached_get", new=_cached_get(garbage)):
            self.assertIsNone(await titles.fetch_title(SETTINGS, 0, Media.SHOW))

    async def test_a_source_failure_is_not_found_rather_than_raised(self):
        """The caller (the enrichment drain) treats a real failure and an
        unrecognised id identically — both back off the same way."""
        spy = AsyncMock(side_effect=transport.SimklError("boom"))
        with patch("app.providers.simkl.transport.cached_get", new=spy):
            self.assertIsNone(await titles.fetch_title(SETTINGS, 1, Media.SHOW))

    async def test_a_missing_id_or_unmapped_media_costs_no_request(self):
        spy = _cached_get()
        with patch("app.providers.simkl.transport.cached_get", new=spy):
            self.assertIsNone(await titles.fetch_title(SETTINGS, None, Media.SHOW))
        spy.assert_not_awaited()

    async def test_a_title_with_no_runtime_or_genres_answers_with_every_key(self):
        payload = {"title": "X", "ids": {"simkl": 1}}
        with patch("app.providers.simkl.transport.cached_get", new=_cached_get(payload)):
            fields = await titles.fetch_title(SETTINGS, 1, Media.SHOW)
        self.assertEqual(set(fields), {
            "extract_version", "genres", "network", "country", "certification",
            "runtime", "status", "overview", "ids", "type", "anime_type",
            "total_episodes", "poster", "first_aired", "trailers",
            "language", "year", "rating", "release_types_by_country",
            "released", "director", "budget",
        })
        self.assertEqual(
            (fields["genres"], fields["runtime"], fields["ids"], fields["trailers"]),
            ([], None, {"simkl": "1"}, []))
        # Every movie field answers its own "nothing here" value rather than
        # being absent, so a caller never has to tell "Simkl said nothing" from
        # "this row predates the field" for a row written under this version.
        self.assertEqual(
            (fields["language"], fields["year"], fields["rating"],
             fields["release_types_by_country"], fields["released"],
             fields["director"], fields["budget"]),
            ("", "", None, {}, "", "", None))


MOVIE_PAYLOAD = {
    "title": "Ice Cream Man",
    "ids": {"simkl": 2777813, "tmdb": "1477712", "imdb": "tt36893729"},
    "year": 2026,
    "language": "EN",
    "released": "2026-08-04",
    "director": "Michael Russell Gunn",
    "budget": 4000000,
    "certification": "",
    "ratings": {"simkl": {"rating": 6.5, "votes": 2},
                "imdb": {"rating": 7.9, "votes": 63}},
    "release_dates": [
        {"iso_3166_1": "US", "results": [{"type": 3, "release_date": "2026-08-04"},
                                         {"type": 4, "release_date": "2026-09-02"}]},
        {"iso_3166_1": "br", "results": [{"type": 1, "release_date": "2026-07-30"}]},
    ],
}


class MovieFieldsTests(unittest.TestCase):
    """The movie half of the extraction, driven straight at `_extract` rather
    than through `fetch_title` — it is a pure function of one payload, and the
    transport it would otherwise be reached through is already covered above.

    Measured over 1314 real films from one live August (2026-08-07): language,
    released, year and release_dates are on 100% of them, director 92%, country
    67%, certification 10%, budget 9%, ratings 6%.
    """

    def test_the_movie_fields_the_calendar_files_never_carry_are_kept(self):
        fields = titles._extract(MOVIE_PAYLOAD)
        self.assertEqual(fields["language"], "EN")
        self.assertEqual(fields["year"], 2026)
        self.assertEqual(fields["released"], "2026-08-04")
        self.assertEqual(fields["director"], "Michael Russell Gunn")
        self.assertEqual(fields["budget"], 4000000)

    def test_the_rating_is_simkls_own_and_never_imdbs(self):
        """`Record.rating` is one number shown under one service's mark, and the
        card draws two services' ratings side by side rather than averaging
        them. Borrowing imdb's figure into the field labelled Simkl would be the
        same untruth somewhere quieter."""
        fields = titles._extract(MOVIE_PAYLOAD)
        self.assertEqual(fields["rating"], 6.5)

    def test_a_title_with_no_simkl_rating_answers_none_rather_than_imdbs(self):
        payload = {**MOVIE_PAYLOAD, "ratings": {"imdb": {"rating": 7.9, "votes": 63}}}
        self.assertIsNone(titles._extract(payload)["rating"])

    def test_release_dates_reduce_to_countries_and_types_with_no_dates(self):
        """The reduced form: which markets have a release and in what formats.
        The dates go, because this rule decides WHICH TITLES a viewer sees and
        not which date a card is drawn on — see _release_types_by_country."""
        fields = titles._extract(MOVIE_PAYLOAD)
        self.assertEqual(fields["release_types_by_country"], {"US": [3, 4], "BR": [1]})

    def test_a_country_code_is_upper_cased_and_types_are_deduplicated(self):
        """One title's map has to be ONE value however Simkl happened to order
        or spell the payload, or two identical films filter differently."""
        payload = {**MOVIE_PAYLOAD, "release_dates": [
            {"iso_3166_1": "gb", "results": [{"type": 4, "release_date": "a"},
                                             {"type": 3, "release_date": "b"},
                                             {"type": 4, "release_date": "c"}]},
        ]}
        self.assertEqual(titles._extract(payload)["release_types_by_country"], {"GB": [3, 4]})

    def test_a_release_type_this_app_does_not_know_is_kept_not_dropped(self):
        """A seventh type Simkl adds later must stay visible: a viewer whose
        filter does not name it simply keeps seeing the title, which is the safe
        direction. Dropping it would make the title invisible instead."""
        payload = {**MOVIE_PAYLOAD, "release_dates": [
            {"iso_3166_1": "US", "results": [{"type": 9, "release_date": "a"}]},
        ]}
        self.assertEqual(titles._extract(payload)["release_types_by_country"], {"US": [9]})

    def test_a_malformed_release_block_is_skipped_rather_than_raising(self):
        payload = {**MOVIE_PAYLOAD, "release_dates": [
            "nonsense", {"results": [{"type": 3}]}, {"iso_3166_1": "US", "results": None},
            {"iso_3166_1": "FR", "results": ["nonsense", {"type": 3}]},
        ]}
        self.assertEqual(titles._extract(payload)["release_types_by_country"], {"FR": [3]})

    def test_certification_is_still_kept_and_an_empty_one_is_the_data(self):
        """Roughly 90% of films carry no certification because Simkl does not
        have it (shows are 22%, anime 90%). Pinned so a blank chip on a movie
        card is never "fixed" into something invented."""
        self.assertEqual(titles._extract(MOVIE_PAYLOAD)["certification"], "")
        self.assertEqual(
            titles._extract({**MOVIE_PAYLOAD, "certification": "R"})["certification"], "R")

    def test_fanart_and_alt_titles_are_not_stored(self):
        """The author declined fanart by name; alt_titles has no reader. Both
        are on a real fraction of films, so the whitelist is the only thing
        keeping them out."""
        payload = {**MOVIE_PAYLOAD, "fanart": "abc123",
                   "alt_titles": [{"name": "x"} for _ in range(50)]}
        fields = titles._extract(payload)
        self.assertNotIn("fanart", fields)
        self.assertNotIn("alt_titles", fields)


class AnimeRedirectTests(unittest.IsolatedAsyncioTestCase):
    """Regression for the 62-of-260-empty-payloads defect: measured live
    2026-08-06, GET /tv/{id} 302s to GET /anime/{id} for a real fraction of
    anime ids (100% of a 94-id sample pulled from the author's live
    database). Driven through a REAL httpx.AsyncClient with a MockTransport
    — genuine redirect-following, no socket — rather than patching
    cached_get, because the bug lived in whether the client follows the
    Location header at all, one layer below cached_get's own logic."""

    async def test_a_redirected_tv_lookup_still_answers(self):
        anime_body = {
            "title": "GuAn", "ids": {"simkl": 3198578, "mal": "62789"},
            "genres": ["Action", "Fantasy"], "country": "CN",
            "certification": "PG-13", "network": "Youku", "status": "airing",
            "runtime": 30, "overview": "A crippled youth seizes divine power.",
        }

        def handler(request):
            if request.url.path == "/tv/3198578":
                return httpx.Response(
                    302, headers={"location": "/anime/3198578?client_id=cid"})
            if request.url.path == "/anime/3198578":
                return httpx.Response(200, json=anime_body)
            raise AssertionError(f"unexpected path {request.url.path}")

        client = httpx.AsyncClient(follow_redirects=True,
                                   transport=httpx.MockTransport(handler))
        with patch("app.providers.simkl.transport.catalog_client", return_value=client), \
             patch("app.cache.get", AsyncMock(return_value=None)), \
             patch("app.cache.set", AsyncMock()):
            fields = await titles.fetch_title(SETTINGS, 3198578, Media.SHOW)
        self.assertIsNotNone(fields)
        self.assertEqual(fields["genres"], ["action", "fantasy"])
        self.assertEqual(fields["country"], "CN")
        self.assertEqual(fields["network"], "Youku")

    async def test_a_client_that_does_not_follow_redirects_would_lose_it(self):
        """The other half of the same proof: without follow_redirects, the
        bare 302 reads as a non-200 and the title is treated as unanswerable
        — this is the defect as it shipped, pinned so it cannot regress."""
        def handler(request):
            return httpx.Response(302, headers={"location": "/anime/3198578?client_id=cid"})

        client = httpx.AsyncClient(follow_redirects=False,
                                   transport=httpx.MockTransport(handler))
        with patch("app.providers.simkl.transport.catalog_client", return_value=client), \
             patch("app.cache.get", AsyncMock(return_value=None)), \
             patch("app.cache.set", AsyncMock()):
            fields = await titles.fetch_title(SETTINGS, 3198578, Media.SHOW)
        self.assertIsNone(fields)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
