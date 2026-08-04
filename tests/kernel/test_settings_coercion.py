"""Unit tests for Settings.from_dict's type coercion (app/config.py).

Everything reaching from_dict may be a string — an HTML form sends "300" and
"on", and a hand-edited settings.json can hold anything. The coercion is derived
from each field's DECLARED type rather than from a hand-kept list of names, so
these tests are written against that property: they assert the behaviour holds
for EVERY int and bool field the dataclass declares, which is what makes adding
a setting safe without an edit here.
"""
from __future__ import annotations

from app.config import Settings, _scalar_field_types


def _fields_of(kind: type) -> list[str]:
    return [name for name, t in _scalar_field_types().items() if t is kind]


class TestDerivedCoercion:
    def test_the_derivation_finds_both_kinds_of_scalar(self):
        """A guard on the tests below: if the derivation ever returned nothing,
        every "for each field" assertion would pass vacuously."""
        assert _fields_of(int)
        assert _fields_of(bool)

    def test_every_int_field_accepts_a_numeric_string(self):
        for name in _fields_of(int):
            assert getattr(Settings.from_dict({name: "42"}), name) == 42

    def test_every_bool_field_reads_a_form_checkbox(self):
        for name in _fields_of(bool):
            assert getattr(Settings.from_dict({name: "on"}), name) is True

    def test_a_bool_field_is_not_coerced_through_int(self):
        """bool is a subclass of int in Python, so a coercion that tested the
        types rather than the annotations would turn every flag into 0 or 1 —
        and "0" is a truthy string, which is the bug that produces."""
        for name in _fields_of(bool):
            assert getattr(Settings.from_dict({name: "0"}), name) is False

    def test_an_uncoercible_int_falls_back_to_the_declared_default(self):
        """Dropped rather than zeroed, so the dataclass's own default is the one
        answer for what a bad value means."""
        assert Settings.from_dict({"pagination_limit": "not a number"}).pagination_limit \
            == Settings().pagination_limit

    def test_a_string_field_is_left_alone(self):
        assert Settings.from_dict({"timezone": "Europe/Athens"}).timezone == "Europe/Athens"


class TestConfiguredProperties:
    def test_trakt_configured_needs_both_halves_of_the_credential(self):
        assert not Settings().trakt_configured
        assert not Settings(trakt_client_id="id").trakt_configured
        assert not Settings(trakt_access_token="token").trakt_configured
        assert Settings(trakt_client_id="id", trakt_access_token="token").trakt_configured

    def test_trakt_catalogue_configured_is_the_client_id_alone(self):
        """The PUBLIC question, and it is deliberately a different one.

        Trakt's catalogue endpoints authenticate with the instance's client id;
        only /sync/ wants a per-person bearer. Asking the private question in
        front of a public read made instance-wide, globally-cached data depend
        on one account's token — an account signed in with another service saw
        every roster row fail.
        """
        assert not Settings().trakt_catalogue_configured
        assert Settings(trakt_client_id="id").trakt_catalogue_configured
        assert not Settings(trakt_access_token="token").trakt_catalogue_configured

    def test_the_private_question_did_not_quietly_widen(self):
        """The point of two properties rather than one loosened one: every sync
        gate still demands the token."""
        assert Settings(trakt_client_id="id").trakt_catalogue_configured
        assert not Settings(trakt_client_id="id").trakt_configured

    def test_calendar_source_configured_tracks_the_registry(self):
        """Not a second spelling of trakt_configured: it asks the registry
        whether ANY source can supply a calendar, which is the question the
        calendar route needs and the one that survives a second provider."""
        assert not Settings().calendar_source_configured
        assert Settings(trakt_client_id="id",
                        trakt_access_token="token").calendar_source_configured
