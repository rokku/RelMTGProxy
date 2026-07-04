"""Tests for `proxy_studio.decklist` — covers every line format in spec §3."""

from __future__ import annotations

import pytest

from proxy_studio.decklist import (
    DeckEntry, DecklistError, parse_line, parse_text,
)


class TestSingleLines:
    def test_bare_quantity_and_name(self):
        assert parse_line("1 Mazirek, Kraul Death Priest") == DeckEntry(
            quantity=1, name="Mazirek, Kraul Death Priest")

    def test_quantity_with_x_suffix(self):
        assert parse_line("1x Sol Ring") == DeckEntry(quantity=1, name="Sol Ring")

    def test_paren_set_and_cn(self):
        assert parse_line("4 Llanowar Elves (M19) 314") == DeckEntry(
            quantity=4, name="Llanowar Elves", set_code="m19",
            collector_number="314")

    def test_bracket_set_and_cn(self):
        assert parse_line("1 Fabled Passage [ELD] 244") == DeckEntry(
            quantity=1, name="Fabled Passage", set_code="eld",
            collector_number="244")

    def test_paren_set_no_cn(self):
        assert parse_line("2 Forest (M21)") == DeckEntry(
            quantity=2, name="Forest", set_code="m21")

    def test_angle_bracket_decoration_stripped(self):
        assert parse_line("2 Swamp <foil>") == DeckEntry(quantity=2, name="Swamp")

    def test_asterisk_foil_marker_stripped(self):
        assert parse_line("1 Sol Ring *F*") == DeckEntry(quantity=1, name="Sol Ring")

    def test_archidekt_annotation_stripped(self):
        assert parse_line("1 Mazirek, Kraul Death Priest #!Commander") == DeckEntry(
            quantity=1, name="Mazirek, Kraul Death Priest")

    def test_blank_line_returns_none(self):
        assert parse_line("") is None
        assert parse_line("   ") is None

    def test_comment_lines_returned_none(self):
        assert parse_line("// deck by X") is None
        assert parse_line("# note") is None

    def test_section_header_returned_none(self):
        assert parse_line("Sideboard:") is None
        assert parse_line("COMMANDER") is None
        assert parse_line("Mainboard") is None

    def test_collector_number_with_star(self):
        # Rare printings can use non-numeric CNs.
        assert parse_line("1 Mox Ruby (LEA) ★") == DeckEntry(
            quantity=1, name="Mox Ruby", set_code="lea",
            collector_number="★")

    def test_unparseable_line_raises(self):
        with pytest.raises(ValueError):
            parse_line("this is not a valid line")


class TestParseText:
    def test_full_document_merges_duplicates(self):
        text = """
        // My deck
        1 Sol Ring
        1x Sol Ring
        4 Llanowar Elves (M19) 314

        SIDEBOARD:
        2 Duress
        """
        entries = parse_text(text)
        # Sol Ring merged; Llanowar Elves has a pinned printing so distinct;
        # Duress in its own bucket.
        names = [(e.quantity, e.name, e.set_code) for e in entries]
        assert (2, "Sol Ring", None) in names
        assert (4, "Llanowar Elves", "m19") in names
        assert (2, "Duress", None) in names
        assert len(entries) == 3

    def test_different_printings_stay_separate(self):
        text = "1 Sol Ring (C15) 234\n1 Sol Ring (C21) 350\n"
        entries = parse_text(text)
        assert len(entries) == 2
        assert {e.set_code for e in entries} == {"c15", "c21"}

    def test_reports_all_failures_with_line_numbers(self):
        text = "1 Sol Ring\n???\n2 Swamp\ngibberish\n"
        with pytest.raises(DecklistError) as excinfo:
            parse_text(text)
        failures = excinfo.value.failures
        assert [n for n, _ in failures] == [2, 4]
