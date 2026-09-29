"""The tracker's month as MARKUP, and every control that answers with it.

WHAT THIS FILE IS FOR. The month used to be JSON the browser assembled into
rows; it is a fragment now, drawn from the same payload
(`_distrakt_month_payload`), and every mutation answers with the same markup
through one helper. So what a viewer is TOLD is testable for the first time —
which section a row landed under, what a refusal says, which control a row
offers — and the tests that used to read a payload for those answers were
reading the ingredients rather than the meal.

WHAT IS NOT HERE. Whether a season belongs in Cleanup, whether a verdict has
lost its backing, what a count string says: those are the payload's, and they
are asked of `tracker_month` in the files that already own them. This file is
about the drawing and about the refusals.

No network: every month here is either never-tracked (which reaches nothing) or
computed with the whole provider surface patched out.
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

from app import distrakt as distrakt_store
from app.config import Settings
from app.providers.base import ItemKey, Media, PlayCounts
from html import unescape

from tests.support import AppTestCase, ORIGIN, TrackerMonth

KEY = ItemKey("show", "tmdb", "500")


def _month_offset(today: date, offset: int) -> tuple[int, int]:
    """The (year, month) `offset` months from `today`.

    Derived rather than written down: a literal month agrees with the real
    calendar for a few weeks and then starts failing on a rule nobody changed.
    """
    index = today.year * 12 + (today.month - 1) + offset
    return divmod(index, 12)[0], divmod(index, 12)[1] + 1


class MonthFragmentTestCase(AppTestCase):
    """A tracker account with every provider read stubbed away."""

    def make_settings(self):
        # No calendar source: these are about what a month DRAWS, and a month
        # with premieres to import would go and read one.
        return Settings(public_base_url=ORIGIN, trakt_client_id="cid",
                        simkl_public_calendar_enabled=False)

    def setUp(self):
        super().setUp()
        self.user_id = self.make_user("tracker", distrakt_approved=True,
                                      calendar_approved=True)
        self.link_identity(self.user_id, "trakt", 900, "tok")
        self.sign_in_as(self.user_id)
        self.today = date.today()
        self.current = _month_offset(self.today, 0)
        self.past = _month_offset(self.today, -6)

    def offline(self):
        """Every outbound read a month makes, replaced.

        The season lookup answers a season still airing, so a row that lands on
        the viewer's list stays there rather than being settled out from under
        the assertion.
        """
        async def season(settings, source_id, season_no, fresh=False, client=None):
            return {"total": 10, "cadence": "Tue", "premiere": "7/1", "finale": "9/2",
                    "started_airing": True, "finished_airing": False}

        return patch.multiple(
            "app.providers.trakt.sync",
            fetch_last_activities=AsyncMock(return_value={
                "episodes": {"watched_at": "T1", "removed_at": None},
                "movies": {"watched_at": "T1", "removed_at": None}}),
            fetch_history=AsyncMock(return_value=[]),
            fetch_progress_details=AsyncMock(return_value={}),
            fetch_play_counts=AsyncMock(return_value=PlayCounts({}, False)),
        ), patch("app.providers.trakt.detail.fetch_season_detail", season), \
            patch("app.calendar.cache.read_month", new=AsyncMock(return_value=([], None)))

    def month(self, year: int, month: int) -> TrackerMonth:
        outer, season, calendar = self.offline()
        with outer, season, calendar:
            resp = self.client.get(
                f"/distrakt/fragments/month?year={year}&month={month}")
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        return TrackerMonth(resp.text)

    def list_one(self, title="Listed Show", season=1, key=KEY):
        """One season on the viewer's own list — what the month under way draws."""
        asyncio.run(distrakt_store.add_user_record(self.user_id, {
            "media": Media.SHOW, "ids": {"trakt": 500, "tmdb": 500},
            "season": season, "title": title, "network": "HBO",
            "kind": distrakt_store.RecordKind.KEEPUP,
        }))

    def _drawn(self, payload: dict) -> str:
        """The month fragment drawn straight off a payload, with no route and no
        read: for what a payload the harness cannot produce looks like."""
        from app.distrakt import routes as distrakt_routes
        emojis, default_emoji = asyncio.run(distrakt_store.get_emoji_prefs(self.user_id))
        context = distrakt_routes._month_context(
            None, {"ok": True, "shows": [], **payload}, *self.current,
            emojis, default_emoji, Settings())
        from app.templating import templates
        return templates.get_template("_distrakt_month.html").render(**context)


# A row the services disagree about, as the payload carries it.
SPLIT_ROW = {"key": str(KEY), "season": 1, "title": "Split Show", "bucket": "cleanup",
             "network": "HBO", "watched": 2, "total": 2,
             "count_label": "2/2", "count_split": True,
             "counts": "2/2 (Trakt) · 3/2 (Simkl)",
             "counts_detail": "Trakt: 2 of 2\nSimkl: 3 of 2"}


class TheRowsCountTests(MonthFragmentTestCase):
    """What a row's count cell draws."""

    def test_a_split_count_is_one_marked_number_with_the_story_in_its_tooltip(self):
        html = self._drawn({"shows": [SPLIT_ROW]})
        self.assertIn('class="distrakt-counts is-split" title="Trakt: 2 of 2\nSimkl: 3 of 2">(2/2)<',
                      html)
        self.assertNotIn("(Trakt)", html.split('id="post1Text"')[0])

    def test_an_agreed_count_is_unmarked(self):
        html = self._drawn({"shows": [{**SPLIT_ROW, "count_split": False, "counts": "2/2"}]})
        self.assertIn('class="distrakt-counts" title=', html)
        self.assertIn(">(2/2)<", html)


class WhatEachStandingDrawsTests(MonthFragmentTestCase):
    """The three months a viewer can be looking at."""

    def test_the_month_under_way_draws_its_rows_under_their_section(self):
        self.list_one()
        drawn = self.month(*self.current)
        self.assertIn("Keepup", drawn.headings)
        # Keepup groups by air weekday, which is discord_fmt's own list and the
        # same grouping the second notice writes.
        self.assertIn("Tue", drawn.headings)
        self.assertEqual(drawn.section("Tue"), ["Listed Show"])
        self.assertEqual(drawn.banner, "")

    def test_a_frozen_month_says_so_and_offers_no_abandon(self):
        """What a past month RECORDS can still be corrected — the ✕ stays — but
        its verdicts were settled when it froze and are not up for re-deciding."""
        year, month = self.past
        key = distrakt_store.month_key(year, month)
        asyncio.run(distrakt_store.add_month_record(self.user_id, key, {
            "media": Media.SHOW, "ids": {"trakt": 500, "tmdb": 500}, "season": 1,
            "title": "Frozen Show", "network": "HBO",
            "kind": distrakt_store.RecordKind.ABANDONED, "watched": 2, "total": 10}))
        doc = asyncio.run(distrakt_store.load_month(self.user_id, key))
        asyncio.run(distrakt_store.save_month(self.user_id, {**doc, "closed": True}))

        drawn = self.month(year, month)
        self.assertEqual(drawn.titles, ["Frozen Show"])
        self.assertIn("frozen snapshot", drawn.banner)
        self.assertNotIn("/api/distrakt/abandon", drawn.posts_to())
        self.assertIn(f"/api/distrakt/remove?year={year}&month={month}",
                      [unescape(url) for url in drawn.posts_to()])

    def test_a_never_tracked_past_month_says_which_kind_of_empty_it_is(self):
        """The same blank list means "waiting for you to ask", "the calendar
        isn't there yet" and "nobody was tracking that far back". Nothing is
        created by looking at one.

        THE ACCOUNT HAS TO HOLD A MONTH ALREADY for this to be the state: a store
        with no months at all is a first seed and may start wherever the user
        starts it (rollover.can_initialize), so a brand-new account reaches this
        month as ordinary and empty rather than as read-only.
        """
        asyncio.run(distrakt_store.add_month_record(
            self.user_id, distrakt_store.month_key(*self.current), {
                "media": Media.SHOW, "ids": {"trakt": 1, "tmdb": 1}, "season": 1,
                "title": "Seed", "network": "HBO",
                "kind": distrakt_store.RecordKind.SERIES_PREMIERE}))
        year, month = _month_offset(self.today, -18)
        drawn = self.month(year, month)
        self.assertEqual(drawn.titles, [])
        self.assertIn("never tracked", drawn.banner)
        self.assertNotIn(distrakt_store.month_key(year, month),
                         asyncio.run(distrakt_store.list_months(self.user_id)))


class TheRowsControlsTests(MonthFragmentTestCase):
    """Every button a row draws, and what it sends."""

    def setUp(self):
        super().setUp()
        self.list_one()
        self.drawn = self.month(*self.current)

    def test_the_row_opens_the_details_panel_and_carries_no_opener_script(self):
        year, month = self.current
        # The month on screen rides in the address: the route looks the row up
        # from it, so a premiere on a month still ahead is found.
        self.assertIn(f'data-details-open="/distrakt/fragments/details?year={year}&amp;month={month}'
                      f'&amp;key=show%3Atmdb%3A500&amp;season=1"', self.drawn.html)
        self.assertIn('data-details-heading="Listed Show · S01"', self.drawn.html)
        self.assertNotIn("onclick", self.drawn.html)

    def test_every_button_posts_json_and_names_the_month_in_its_address(self):
        year, month = self.current
        for url in (unescape(u) for u in self.drawn.posts_to()):
            with self.subTest(url=url):
                if url.endswith(("acknowledge-return", "unknown-dismiss")):
                    continue  # in-place removals; they act on a row, not a month
                self.assertIn(f"?year={year}&month={month}", url)
        self.assertIn("hx-json-enc", self.drawn.html)

    def test_the_removal_asks_first_and_the_question_is_markup(self):
        """One control that turns into its own confirmation and back, with no
        request and no script — templates/_confirm.html."""
        self.assertIn('class="confirm-toggle"', self.drawn.html)
        self.assertIn("Remove this show from the tracker for this month?",
                      self.drawn.html)


class TheQuestionsAboveTheListTests(MonthFragmentTestCase):
    """The three questions the page can ask, drawn.

    A HISTORY QUESTION CANNOT BE RAISED WITHOUT A LIVE PULL, which is why the
    browser pass could not reach these: they come out of a watch-history read,
    and a harness with no reachable service takes the degraded path. So the row
    and both its answers are pinned here, off the payload the fragment draws
    from.
    """

    def test_a_play_nothing_could_place_names_the_episode_and_asks(self):
        html = self._drawn({"unknown_episodes": [
            {"key": str(KEY), "season": 2, "number": 4, "title": "Never Heard Of It",
             "ids": {"tmdb": 500}}]})
        self.assertIn("You watched <strong>Never Heard Of It S02E04</strong>", html)
        self.assertIn("Should we add this to Distrakt?", html)
        self.assertIn("/api/distrakt/unknown-add", html)
        self.assertIn("/api/distrakt/unknown-dismiss", html)

    def test_a_season_given_up_on_asks_its_own_question(self):
        html = self._drawn({"given_up_episodes": [
            {"key": str(KEY), "season": 2, "number": 4, "title": "Gave Up On It",
             "ids": {}}]})
        self.assertIn("Add it back to Distrakt?", html)
        self.assertIn("/api/distrakt/unknown-resume", html)

    def test_a_verdict_nothing_backs_carries_the_servers_own_sentence(self):
        """The odd one out: not raised by a play, so there is no episode to name
        and the sentence is a comparison only the server holds both halves of."""
        html = self._drawn({"unbacked_verdicts": [
            {"key": str(KEY), "season": 1, "title": "Was Finished",
             "note": "Trakt no longer reports finishing this."}]})
        self.assertIn("Trakt no longer reports finishing this.", html)
        self.assertIn("/api/distrakt/verdict-readd", html)

    def test_only_the_verdict_asks_before_it_acts(self):
        """The other two ADD something and the row's own remove undoes them;
        this one takes a verdict apart and the month's record of having finished
        that season does not survive it."""
        plain = self._drawn({"unknown_episodes": [
            {"key": str(KEY), "season": 2, "number": 4, "title": "A", "ids": {}}]})
        verdict = self._drawn({"unbacked_verdicts": [
            {"key": str(KEY), "season": 1, "title": "B", "note": "n"}]})
        self.assertNotIn("confirm-toggle", plain.split("distrakt-unknown-row")[1]
                         .split("</div>")[0] + plain.split("distrakt-row-actions")[1]
                         .split("</span>")[0])
        self.assertIn("This month stops recording that you finished it.", verdict)

    def test_a_rewatch_offer_carries_its_sentence_and_can_be_put_off(self):
        html = self._drawn({"rewatch_offers": [
            {"key": str(KEY), "season": 1, "title": "Seen It",
             "note": "finished in February 2026, and you've watched E04 again."}]})
        self.assertIn("<strong>Seen It S01</strong> — finished in February 2026", html)
        self.assertIn("/api/distrakt/rewatch-start", html)
        self.assertIn("/api/distrakt/unknown-snooze", html)
        self.assertIn("Not this month", html)

    def test_a_finale_offer_records_a_completion(self):
        html = self._drawn({"finale_offers": [
            {"key": str(KEY), "season": 1, "title": "Seen It", "note": "n"}]})
        self.assertIn("/api/distrakt/rewatch-complete", html)
        self.assertIn("/api/distrakt/unknown-snooze", html)

    def test_only_the_rewatch_offers_can_be_put_off(self):
        """An occasional episode watched again is the question somebody may not
        want every evening; the others are asked once per viewing anyway."""
        for field in ("unknown_episodes", "given_up_episodes", "unbacked_verdicts"):
            with self.subTest(field=field):
                html = self._drawn({field: [
                    {"key": str(KEY), "season": 1, "number": 1, "title": "T",
                     "ids": {}, "note": "n"}]})
                self.assertNotIn("/api/distrakt/unknown-snooze", html)

    def test_the_three_share_one_refusal(self):
        """Saying no is one statement whichever question it was — do not put this
        season on my list, stop asking — so every one sends the same dismissal."""
        for field in ("unknown_episodes", "given_up_episodes", "unbacked_verdicts",
                      "rewatch_offers", "finale_offers"):
            with self.subTest(field=field):
                html = self._drawn({field: [
                    {"key": str(KEY), "season": 1, "number": 1, "title": "T",
                     "ids": {}, "note": "n"}]})
                self.assertEqual(html.count("/api/distrakt/unknown-dismiss"), 1)


class EveryConvertedRouteRefusesTests(MonthFragmentTestCase):
    """The three refusals every converted route owes, asked of all of them at
    once: a body that is not JSON, another account's row, and a value that
    cannot be read.

    ENUMERATED RATHER THAN DISCOVERED. A route that quietly stops being covered
    here should fail this file rather than drop out of it.
    """

    # (path, a body that names a row).
    ROW_ROUTES = (
        ("/api/distrakt/remove", {"key": str(KEY), "season": 1}),
        ("/api/distrakt/abandon", {"key": str(KEY), "season": 1, "abandoned": True}),
        ("/api/distrakt/acknowledge-return", {"key": str(KEY), "season": 1}),
        ("/api/distrakt/unknown-add", {"key": str(KEY), "season": 1}),
        ("/api/distrakt/unknown-resume", {"key": str(KEY), "season": 1}),
        ("/api/distrakt/verdict-readd", {"key": str(KEY), "season": 1}),
        ("/api/distrakt/rewatch-start", {"key": str(KEY), "season": 1}),
        ("/api/distrakt/rewatch-complete", {"key": str(KEY), "season": 1}),
        ("/api/distrakt/remove-movie", {"key": "movie:tmdb:7"}),
    )
    OTHER_ROUTES = (
        # A DISMISSAL IS NOT A ROW ROUTE, and that is the point of it being here
        # instead: it writes a refusal against THIS account and reads nobody
        # else's records at all, so naming a season somebody else tracks changes
        # nothing for them. It still owes the other two refusals.
        ("/api/distrakt/unknown-dismiss", {"key": str(KEY), "season": 1}),
        ("/api/distrakt/unknown-snooze", {"key": str(KEY), "season": 1}),
        ("/api/distrakt/refresh", {}),
        ("/api/distrakt/import", {}),
        ("/api/distrakt/add", {"q": "x", "i": 0, "season": 1}),
        ("/api/distrakt/add-completed", {"q": "x", "i": 0, "season": 1}),
        ("/api/distrakt/add-movie", {"q": "x", "i": 0, "watched_on": "2026-01-01"}),
        ("/api/distrakt/emojis", {"network-0": "HBO", "emoji-0": ":hbo:"}),
        ("/api/distrakt/emoji", {"network": "HBO", "code": ":hbo:"}),
        ("/api/distrakt/emoji-default", {"code": ":tv:"}),
        ("/api/distrakt/network-logo", {"network": "HBO"}),
        ("/api/distrakt/backfill/survey", {"start": "2026-01", "end": "2026-02"}),
        ("/api/distrakt/backfill/apply", {}),
        ("/api/distrakt/share-link", {"kind": ""}),
        ("/api/distrakt/restore", {"payload": "{}", "ack": "REPLACE MY DATA"}),
        ("/distrakt/fragments/emoji-rows", {}),
    )

    def test_a_form_encoded_body_is_refused_before_routing(self):
        """CSRF defence-in-depth behind the session cookie's SameSite=Lax: a
        cross-origin HTML form can only send form-encoded or text/plain, so the
        check makes that POST unreachable. Enforced app-wide by
        `request_shape_guard`, which is why every one of these is the same 415
        and why no route re-checks it."""
        for path, body in self.ROW_ROUTES + self.OTHER_ROUTES:
            with self.subTest(path=path):
                resp = self.client.post(path, data={k: str(v) for k, v in body.items()})
                self.assertEqual(resp.status_code, 415, resp.text[:200])

    def test_a_row_on_another_account_is_refused_and_left_alone(self):
        """The key is a flat string the page was handed, so a caller can spell
        one naming somebody else's row. Every route resolves it against THIS
        session's own records and finds nothing."""
        neighbour = self.make_user("neighbour", distrakt_approved=True,
                                   calendar_approved=True)
        self.link_identity(neighbour, "trakt", 901, "tok2")
        asyncio.run(distrakt_store.add_user_record(neighbour, {
            "media": Media.SHOW, "ids": {"trakt": 500, "tmdb": 500}, "season": 1,
            "title": "Not Yours", "network": "HBO",
            "kind": distrakt_store.RecordKind.KEEPUP}))
        year, month = self.current

        for path, body in self.ROW_ROUTES:
            with self.subTest(path=path):
                outer, season, calendar = self.offline()
                with outer, season, calendar:
                    resp = self.client.post(f"{path}?year={year}&month={month}", json=body)
                self.assertEqual(resp.status_code, 200, resp.text[:200])
                self.assertIn("toast err", resp.text, path)
        # And the neighbour's row is exactly where it was.
        theirs = asyncio.run(distrakt_store.find_user_record(neighbour, KEY, 1))
        self.assertIsNotNone(theirs, "another account's row was written to")
        self.assertEqual(theirs["title"], "Not Yours")

    def test_a_value_that_cannot_be_read_is_refused_and_drawn(self):
        """Drawn at 200 with the sentence out of band, like every refusal on this
        page: a non-200 is never swapped (static/js/boost.js's `noSwap`), so a
        status code would be a control that silently did nothing."""
        year, month = self.current
        bad = (
            ("/api/distrakt/remove", {"key": "nonsense", "season": 1}),
            ("/api/distrakt/abandon", {"key": str(KEY), "season": "later"}),
            ("/api/distrakt/acknowledge-return", {"key": "", "season": 1}),
            ("/api/distrakt/unknown-dismiss", {"key": "show:letterboxd:1", "season": 1}),
            ("/api/distrakt/remove-movie", {"key": "movie:nope:7"}),
            ("/api/distrakt/add", {"q": "x", "i": "not-a-place", "season": 1}),
            ("/api/distrakt/add-movie", {"q": "x", "i": 0, "watched_on": "the 4th"}),
            ("/api/distrakt/share-link", {"kind": "carrier-pigeon"}),
            ("/api/distrakt/restore", {"payload": "not json", "ack": "REPLACE MY DATA"}),
        )
        for path, body in bad:
            with self.subTest(path=path):
                outer, season, calendar = self.offline()
                with outer, season, calendar:
                    resp = self.client.post(f"{path}?year={year}&month={month}", json=body)
                self.assertEqual(resp.status_code, 200, resp.text[:200])
                self.assertIn("distrakt-warn" if "restore" in path else "toast err",
                              resp.text, path)


class TheAddOverlaysCarryTheMonthTests(MonthFragmentTestCase):
    """THE FIRST LINK IN THE ADD CHAIN, and the one that broke it.

    Which month a control acts on is a REFERENCE the server resolves from the
    ADDRESS a request arrives on (routes._viewed_month), never from the body. The
    add flow is four requests long — search, results, seasons, the post — and
    each one builds the next one's address, so the month has to be in the FIRST
    of them or every link after it silently falls back to today.

    IT WAS NOT, AND THE FAILURE WAS INVISIBLE. The overlays are `{% import %}`ed
    macros, and a Jinja import deliberately does NOT carry the caller's context,
    so `month_query` rendered empty while every other control on the page had
    it. Filling in a past month therefore searched, listed and posted against
    TODAY: the add ran the current month's re-watch evaluation and put the row on
    the viewer's own list instead of recording it finished on the month they were
    looking at, and adding a film redrew today's month over the one on screen.
    Both read as the month being ignored, which is why this asserts the ADDRESS
    rather than the outcome — the outcome has several ways to look right.
    """

    def _page(self, year: int, month: int) -> str:
        outer, season, calendar = self.offline()
        with outer, season, calendar:
            return self.client.get(f"/distrakt?year={year}&month={month}").text

    def test_the_show_search_names_the_month_on_screen(self):
        past_y, past_m = self.past
        html = self._page(past_y, past_m)
        self.assertIn(
            f'hx-get="/distrakt/fragments/search?year={past_y}'
            f'&amp;month={past_m}"', html)

    def test_the_film_search_names_the_month_on_screen(self):
        past_y, past_m = self.past
        html = self._page(past_y, past_m)
        self.assertIn(
            f'hx-get="/distrakt/fragments/search-movie?year={past_y}'
            f'&amp;month={past_m}"', html)

    def test_the_month_answer_carries_it_too(self):
        """The overlays come back with every month answer, closed and empty, so
        an answer that redrew them without the month would undo this on the first
        thing anybody pressed."""
        past_y, past_m = self.past
        outer, season, calendar = self.offline()
        with outer, season, calendar:
            resp = self.client.get(
                f"/distrakt/fragments/month?year={past_y}&month={past_m}")
        self.assertIn(f"search?year={past_y}&amp;month={past_m}",
                      resp.text)

    def test_the_current_month_names_itself_rather_than_nothing(self):
        """Not a special case: today is a month like any other, and leaving the
        address bare is what made every month look like today."""
        now_y, now_m = self.current
        html = self._page(now_y, now_m)
        self.assertIn(
            f'hx-get="/distrakt/fragments/search?year={now_y}'
            f'&amp;month={now_m}"', html)


class TheAnswersOneHelperGivesTests(MonthFragmentTestCase):
    """Every mutation answers with the month through `_month_answer`, so the
    parts of the page that are not the list travel with it."""

    def test_the_answer_redraws_the_notice_the_banner_and_both_copy_blocks(self):
        self.list_one()
        year, month = self.current
        outer, season, calendar = self.offline()
        with outer, season, calendar:
            resp = self.client.post(f"/api/distrakt/refresh?year={year}&month={month}",
                                    json={})
        self.assertEqual(resp.status_code, 200)
        for marker in ('id="distraktNotice"', 'id="distraktBanner"',
                       'id="post1Text"', 'id="post2Text"'):
            self.assertIn(f'{marker}', resp.text)
        self.assertEqual(resp.text.count('hx-swap-oob="outerHTML"'),
                         # the notice, the banner, the two copy blocks, the
                         # three overlays the add flow uses, the import dialog
                         # and the Import button whose state is the month's
                         9)

    def test_the_answer_brings_the_add_overlays_back_closed(self):
        """Unconditional rather than per-route: an answer only ever exists
        because somebody pressed something, and every way of pressing one of
        these ends with the overlay having no business being open."""
        year, month = self.current
        outer, season, calendar = self.offline()
        with outer, season, calendar:
            resp = self.client.post(f"/api/distrakt/refresh?year={year}&month={month}",
                                    json={})
        self.assertIn('<dialog id="addShowModal" class="modal-overlay" closedby="any"'
                      ' hx-swap-oob', resp.text)
        self.assertIn('<dialog id="rewatchModal" class="modal-overlay" closedby="any"',
                      resp.text)
        # Neither arrives asking to be shown — that marker is what opens one.
        self.assertNotIn("data-open-modal", resp.text)

    def test_an_in_place_removal_answers_with_nothing_at_all(self):
        """The mark asked to be swapped out of existence, so having nothing to
        swap in is exactly right — and recomputing a month to drop one word
        would cost a season lookup per listed title."""
        self.list_one()
        asyncio.run(distrakt_store.set_came_back(self.user_id, KEY, 1, True))
        resp = self.client.post("/api/distrakt/acknowledge-return",
                                json={"key": str(KEY), "season": 1})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.text, "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
