"""The chip vocabularies and the form-field encoding the filters panel uses.

Pure functions, so none of this needs a database or a client. What is being held
here is the seam between two languages: the panel renders chips from `chips_for`
and the route reads them back with `specs_from_form`, and if those two ever
disagree about a name a chip becomes one nobody can set.
"""
from __future__ import annotations

import unittest

from app.calendar import vocab
from app.providers.base import Media


class FieldNameTests(unittest.TestCase):
    def test_a_name_round_trips_through_its_own_parser(self):
        """THE WHOLE POINT OF THE MODULE. One function spells the name and one
        reads it, so a change to the format cannot land in only half the app."""
        for field in sorted(vocab.FIELDS):
            with self.subTest(field=field):
                name = vocab.field_name(field, "drama")
                self.assertEqual(vocab.parse_field_name(name), (field, "drama"))

    def test_a_name_it_does_not_recognize_is_not_a_chip(self):
        """A payload legitimately carries fields that are not chips — the pause
        switch, the service boxes — and a name from a newer version of the panel
        must not stop an older server saving the rest."""
        for name in ("filters_paused", "sources_show", "chip:not_a_field:x",
                     "chip:tv_genres:", "chip:tv_genres", "", "chip"):
            with self.subTest(name=name):
                self.assertIsNone(vocab.parse_field_name(name))

    def test_a_token_carrying_the_separator_is_refused(self):
        """It would produce a name that reads as a different field, so it is
        refused rather than misfiled — the panel refuses it in the browser too,
        where the person can still see what they typed."""
        with self.assertRaises(ValueError):
            vocab.field_name(vocab.NETWORK_FILTER, "BBC:Two")


class ChipsForTests(unittest.TestCase):
    def test_a_stored_answer_lights_its_vocabulary_chip(self):
        chips = {c.token: c for c in vocab.chips_for(vocab.TV_GENRES, "drama, -reality")}
        self.assertEqual(chips["drama"].mode, vocab.INCLUDE)
        self.assertEqual(chips["reality"].mode, vocab.EXCLUDE)
        self.assertEqual(chips["comedy"].mode, vocab.IGNORED)

    def test_a_token_the_vocabulary_does_not_name_is_still_drawn(self):
        """Anything typed into the box, and anything a card badge added, has to
        appear — otherwise an answer the viewer gave is invisible in the one
        place that claims to show every answer."""
        chips = vocab.chips_for(vocab.TV_COUNTRIES, "us, -zz")
        extra = [c for c in chips if not c.known]
        self.assertEqual([c.token for c in extra], ["zz"])
        self.assertEqual(extra[0].mode, vocab.EXCLUDE)

    def test_a_stored_token_is_never_drawn_twice(self):
        """The failure a naive "vocabulary, then everything stored" produces: the
        same genre grey in the grid and lit again at the end, disagreeing."""
        tokens = [c.token for c in vocab.chips_for(vocab.TV_GENRES, "drama")]
        self.assertEqual(tokens.count("drama"), 1)

    def test_case_folds_where_the_matching_parser_folds(self):
        """parse_spec lowercases, so "Drama" and "drama" are one token and one
        chip. If this folded differently from the parser the panel would draw a
        second chip for an answer the filter already holds."""
        chips = [c for c in vocab.chips_for(vocab.TV_GENRES, "Drama") if c.mode]
        self.assertEqual([c.token for c in chips], ["drama"])

    def test_a_networks_case_is_load_bearing_and_is_not_folded(self):
        """One week of this calendar carried both TVN and tvN — a Polish
        broadcaster and a Korean one — so for networks alone an exact match is
        the same network."""
        chips = [c.token for c in vocab.chips_for(vocab.NETWORK_FILTER, ["tvN", "-TVN"])]
        self.assertEqual(sorted(chips), ["TVN", "tvN"])

    def test_every_field_has_a_name_prefix_the_panel_can_use(self):
        """The panel hands each add-box `field_name(field, "")` and the browser
        appends the token, which is what keeps the format out of JavaScript."""
        for field in sorted(vocab.FIELDS):
            with self.subTest(field=field):
                prefix = vocab.field_name(field, "")
                self.assertEqual(vocab.parse_field_name(prefix + "x"), (field, "x"))


class SpecsFromFormTests(unittest.TestCase):
    def test_it_writes_the_spec_the_filter_module_would_have_written(self):
        specs = vocab.specs_from_form({
            vocab.field_name(vocab.TV_GENRES, "drama"): "include",
            vocab.field_name(vocab.TV_GENRES, "reality"): "exclude",
        })
        self.assertEqual(specs[vocab.TV_GENRES], "drama, -reality")

    def test_a_chip_set_to_nothing_still_names_its_field(self):
        """This is what makes clearing work. A chip back at grey posts an empty
        value, so the field is mentioned and written empty; a field the payload
        never mentions is a tab that was not open and is left alone."""
        specs = vocab.specs_from_form({vocab.field_name(vocab.TV_GENRES, "drama"): ""})
        self.assertEqual(specs, {vocab.TV_GENRES: ""})

    def test_a_tab_that_was_not_posted_is_absent_rather_than_empty(self):
        specs = vocab.specs_from_form({vocab.field_name(vocab.TV_GENRES, "drama"): "include"})
        self.assertNotIn(vocab.MOVIE_GENRES, specs)

    def test_networks_come_back_a_list_and_everything_else_a_string(self):
        specs = vocab.specs_from_form({
            vocab.field_name(vocab.NETWORK_FILTER, "HBO"): "include",
            vocab.field_name(vocab.TV_COUNTRIES, "us"): "include",
        })
        self.assertEqual(specs[vocab.NETWORK_FILTER], ["HBO"])
        self.assertEqual(specs[vocab.TV_COUNTRIES], "us")

    def test_a_mode_it_cannot_act_on_is_ignored(self):
        specs = vocab.specs_from_form({vocab.field_name(vocab.TV_GENRES, "drama"): "maybe"})
        self.assertEqual(specs, {})

    def test_it_round_trips_what_chips_for_drew(self):
        """The seam, closed: render a stored spec as chips, post those chips back
        unchanged, and get the same spec. Any disagreement between the two halves
        shows up here rather than as a filter that quietly stops matching."""
        stored = "drama, -reality, -zz"
        payload = {c.name: c.mode for c in vocab.chips_for(vocab.TV_GENRES, stored)}
        self.assertEqual(vocab.specs_from_form(payload)[vocab.TV_GENRES], stored)


class ActiveSpecsTests(unittest.TestCase):
    PREFS = {
        "tv_genres": "drama", "tv_countries": "gb",
        "movie_genres": "horror", "movie_countries": "us",
        "show_certifications": "-TV-MA", "movie_certifications": "-R",
        "movie_release_countries": "us", "movie_release_types": "3",
        "network_filter": ["HBO"], "filters_paused": False,
    }

    def test_a_show_read_gets_the_show_columns(self):
        specs = vocab.active_specs(self.PREFS, Media.SHOW, honour_pause=True)
        self.assertEqual(specs["genres"], "drama")
        self.assertEqual(specs["countries"], "gb")
        self.assertEqual(specs["show_certifications"], "-TV-MA")
        self.assertEqual(specs["network_filter"], ["HBO"])
        # The film dimensions travel with every read and must be empty here, or
        # a films-only narrowing would act on a show calendar.
        self.assertEqual(specs["movie_certifications"], "")
        self.assertEqual(specs["movie_release_countries"], "")

    def test_a_film_read_gets_the_film_columns_and_no_network(self):
        """A film has no network at all, so a film read is handed an empty list
        rather than the viewer's show networks."""
        specs = vocab.active_specs(self.PREFS, Media.MOVIE, honour_pause=True)
        self.assertEqual(specs["genres"], "horror")
        self.assertEqual(specs["countries"], "us")
        self.assertEqual(specs["movie_certifications"], "-R")
        self.assertEqual(specs["network_filter"], [])
        self.assertEqual(specs["show_certifications"], "")

    def test_the_switch_empties_every_dimension_without_touching_one(self):
        paused = {**self.PREFS, "filters_paused": True}
        specs = vocab.active_specs(paused, Media.SHOW, honour_pause=True)
        self.assertEqual(specs["genres"], "")
        self.assertEqual(specs["network_filter"], [])
        # Nothing was cleared — the stash is a read-time question only.
        self.assertEqual(paused["tv_genres"], "drama")

    def test_a_share_link_keeps_filtering_while_its_owner_is_paused(self):
        """The stash is a PRIVATE look at your own calendar. An owner who
        switched their filters off for the afternoon must not silently publish a
        wider calendar to everyone holding their link."""
        paused = {**self.PREFS, "filters_paused": True}
        specs = vocab.active_specs(paused, Media.SHOW, honour_pause=False)
        self.assertEqual(specs["genres"], "drama")

    def test_every_read_gets_every_keyword(self):
        """The read path is handed every keyword every time and never has to
        tell "not asked" from "asked for nothing"."""
        for media in (Media.SHOW, Media.MOVIE):
            with self.subTest(media=media):
                specs = vocab.active_specs(self.PREFS, media, honour_pause=True)
                self.assertEqual(set(specs), set(vocab._EMPTY_SPECS))


class CountTests(unittest.TestCase):
    def test_a_tab_counts_tokens_rather_than_dimensions(self):
        """"3" means three things are being filtered. Counting dimensions would
        say "1" for somebody who had excluded nine genres, which reads as though
        almost nothing were set."""
        prefs = {"tv_genres": "drama, -reality, -news", "tv_countries": "",
                 "network_filter": ["HBO"], "show_certifications": ""}
        self.assertEqual(vocab.count_set(prefs, vocab.TV_FIELDS), 4)

    def test_nothing_set_counts_nothing(self):
        prefs = {field: "" for field in vocab.FIELDS}
        prefs[vocab.NETWORK_FILTER] = []
        self.assertEqual(vocab.count_set(prefs, vocab.TV_FIELDS), 0)
        self.assertFalse(vocab.any_set(prefs, vocab.MOVIE_FIELDS))


class TheNewerDimensionsTests(unittest.TestCase):
    """Language, keywords, show type, missing data and the two switches, through
    the same seams as every other dimension."""

    def test_a_keyword_holding_a_comma_round_trips_as_one_keyword(self):
        """A keyword's own name can hold a comma, so keywords are a LIST, and a
        chip for one reads back as exactly that one keyword."""
        name = vocab.field_name(vocab.TV_KEYWORDS, "san francisco, california")
        specs = vocab.specs_from_form({name: vocab.EXCLUDE})
        self.assertEqual(specs[vocab.TV_KEYWORDS], ["-san francisco, california"])
        chips = vocab.chips_for(vocab.TV_KEYWORDS, specs[vocab.TV_KEYWORDS])
        self.assertEqual([(c.token, c.mode) for c in chips],
                         [("san francisco, california", vocab.EXCLUDE)])

    def test_keywords_fold_case_where_networks_keep_it(self):
        chips = vocab.chips_for(vocab.TV_KEYWORDS, ["Anime", "-anime"])
        self.assertEqual(len(chips), 1, "one keyword, whatever its case")
        self.assertEqual(len(vocab.chips_for(vocab.NETWORK_FILTER, ["TVN", "tvN"])), 2)

    def test_a_tick_field_with_nothing_ticked_is_cleared_by_its_presence_marker(self):
        """An unticked box sends nothing; the marker is what makes that an answer."""
        specs = vocab.specs_from_form({vocab.presence_name(vocab.TV_MISSING): ""})
        self.assertEqual(specs[vocab.TV_MISSING], "")
        ticked = vocab.specs_from_form({
            vocab.presence_name(vocab.TV_MISSING): "",
            vocab.field_name(vocab.TV_MISSING, "country+poster"): vocab.INCLUDE})
        self.assertEqual(ticked[vocab.TV_MISSING], "country+poster")

    def test_a_switch_and_its_companion_read_as_one_answer(self):
        name = vocab.toggle_name(vocab.TV_SINGLE_SERVICE)
        self.assertEqual(vocab.toggles_from_form({name: ["0", "1"]}),
                         {vocab.TV_SINGLE_SERVICE: True})
        self.assertEqual(vocab.toggles_from_form({name: "0"}), {vocab.TV_SINGLE_SERVICE: False})
        self.assertEqual(vocab.toggles_from_form({"toggle:not_a_switch": "1"}), {})

    def test_every_dimension_belongs_to_exactly_the_tabs_it_has(self):
        """A film has neither a type nor a network; everything else is per medium."""
        for field in (vocab.TV_SHOW_TYPES, vocab.TV_SPARSE_NO_NETWORK):
            self.assertIn(field, vocab.TV_FIELDS)
            self.assertNotIn(field, vocab.MOVIE_FIELDS)
        for tv, movie in ((vocab.TV_LANGUAGES, vocab.MOVIE_LANGUAGES),
                          (vocab.TV_KEYWORDS, vocab.MOVIE_KEYWORDS),
                          (vocab.TV_MISSING, vocab.MOVIE_MISSING),
                          (vocab.TV_SINGLE_SERVICE, vocab.MOVIE_SINGLE_SERVICE)):
            self.assertIn(tv, vocab.TV_FIELDS)
            self.assertIn(movie, vocab.MOVIE_FIELDS)

    def test_active_specs_turns_each_medium_into_its_own_columns(self):
        prefs = {vocab.TV_LANGUAGES: "en", vocab.MOVIE_LANGUAGES: "-ja",
                 vocab.TV_KEYWORDS: ["-anime"], vocab.MOVIE_KEYWORDS: ["sequel"],
                 vocab.TV_SHOW_TYPES: "-talk-show", vocab.TV_MISSING: "poster",
                 vocab.MOVIE_MISSING: "country", vocab.TV_SINGLE_SERVICE: True,
                 vocab.MOVIE_SINGLE_SERVICE: False, vocab.TV_SPARSE_NO_NETWORK: True}
        tv = vocab.active_specs(prefs, Media.SHOW, honour_pause=True)
        film = vocab.active_specs(prefs, Media.MOVIE, honour_pause=True)
        self.assertEqual((tv["languages"], tv["keywords"], tv["show_types"], tv["missing"],
                          tv["single_service"], tv["sparse_no_network"]),
                         ("en", ["-anime"], "-talk-show", "poster", True, True))
        self.assertEqual((film["languages"], film["keywords"], film["show_types"],
                          film["missing"], film["single_service"], film["sparse_no_network"]),
                         ("-ja", ["sequel"], "", "country", False, False))

    def test_pausing_pauses_the_newer_dimensions_and_a_share_link_does_not(self):
        prefs = {vocab.TV_LANGUAGES: "en", vocab.TV_SINGLE_SERVICE: True,
                 vocab.TV_KEYWORDS: ["-anime"], "filters_paused": True}
        paused = vocab.active_specs(prefs, Media.SHOW, honour_pause=True)
        self.assertEqual(paused, vocab.complete_specs({}))
        shared = vocab.active_specs(prefs, Media.SHOW, honour_pause=False)
        self.assertEqual((shared["languages"], shared["single_service"], shared["keywords"]),
                         ("en", True, ["-anime"]))

    def test_a_read_keyword_nobody_names_is_refused(self):
        with self.assertRaises(TypeError):
            vocab.complete_specs({"langauges": "en"})

    def test_the_floor_reads_only_the_fields_it_declares(self):
        """vocab.FLOOR_FIELDS is what the memo key promises to carry; each must
        change what `floor_specs` answers for some medium."""
        from app.config import Settings
        values = {"keywords": ["-anime"], "network_filter": ["HBO"]}
        for field in vocab.FLOOR_FIELDS:
            with self.subTest(field=field):
                default = Settings()
                value = values.get(field, True if isinstance(getattr(default, field), bool)
                                   else "x")
                changed = Settings(**{field: value})
                self.assertTrue(any(vocab.floor_specs(default, media) != vocab.floor_specs(changed, media)
                                    for media in (Media.SHOW, Media.MOVIE)), field)

    def test_a_language_badge_writes_its_mediums_column(self):
        self.assertEqual(vocab.badge_field("language", "movie"), vocab.MOVIE_LANGUAGES)
        self.assertEqual(vocab.badge_field("language", "show"), vocab.TV_LANGUAGES)
        modes = vocab.BadgeModes({vocab.TV_LANGUAGES: "-ja"})
        self.assertEqual(modes.mode("language", "JA", "show"), vocab.EXCLUDE)
