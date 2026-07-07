"""Tests for PV-name protocol parsing and validation.

Covers the ``PVA://`` incident: the prefix match must be case-insensitive, and
empty / malformed names must be rejected with a clean ``InvalidPVName`` (400)
rather than reaching the EPICS C library.
"""

import pytest

from canopy.epics.pv_protocol import (
    MAX_PV_NAME_LEN,
    InvalidPVName,
    is_ca,
    is_pva,
    is_unprefixed,
    parse_pv_name,
    validate_pv_name,
    strip_protocol_prefix,
)


class TestParsePvName:
    def test_lowercase_pva(self):
        assert parse_pv_name("pva://X:Y") == ("pva", "X:Y")

    def test_lowercase_ca(self):
        assert parse_pv_name("ca://X:Y") == ("ca", "X:Y")

    def test_unprefixed_defaults_to_ca(self):
        assert parse_pv_name("X:Y") == ("ca", "X:Y")

    @pytest.mark.parametrize("name", ["PVA://X:Y", "Pva://X:Y", "pVa://X:Y", "PVA://X:Y"])
    def test_uppercase_pva_is_recognized(self, name):
        # The incident: "PVA://" used to fall through as a literal CA name.
        protocol, stripped = parse_pv_name(name)
        assert protocol == "pva"
        assert stripped == "X:Y"

    @pytest.mark.parametrize("name", ["CA://X:Y", "Ca://X:Y", "cA://X:Y"])
    def test_uppercase_ca_is_recognized(self, name):
        protocol, stripped = parse_pv_name(name)
        assert protocol == "ca"
        assert stripped == "X:Y"


class TestPrefixPredicates:
    def test_is_pva_case_insensitive(self):
        assert is_pva("PVA://X")
        assert is_pva("pva://X")
        assert not is_pva("ca://X")

    def test_is_ca_case_insensitive(self):
        assert is_ca("CA://X")
        assert is_ca("ca://X")
        assert not is_ca("pva://X")

    def test_is_unprefixed(self):
        assert is_unprefixed("PLAIN:PV")
        assert not is_unprefixed("PVA://X")
        assert not is_unprefixed("ca://X")

    def test_strip_case_insensitive(self):
        assert strip_protocol_prefix("PVA://X:Y") == "X:Y"
        assert strip_protocol_prefix("CA://X:Y") == "X:Y"
        assert strip_protocol_prefix("PLAIN:PV") == "PLAIN:PV"


class TestValidatePvName:
    @pytest.mark.parametrize(
        "name",
        [
            "SOME:PV",
            "ca://SOME:PV",
            "pva://SOME:PV",
            "PVA://SOME:PV",
            "MY-DEVICE:field_1.VAL",
            "X" * MAX_PV_NAME_LEN,
        ],
    )
    def test_valid_names_pass_through_unchanged(self, name):
        assert validate_pv_name(name) == name

    def test_bare_pva_prefix_rejected(self):
        # The exact incident string: a prefix with no channel name after it.
        with pytest.raises(InvalidPVName):
            validate_pv_name("pva://")

    @pytest.mark.parametrize("name", ["PVA://", "ca://", "CA://", "   pva://   "])
    def test_bare_prefix_variants_rejected(self, name):
        with pytest.raises(InvalidPVName):
            validate_pv_name(name)

    @pytest.mark.parametrize("name", ["", "   ", "\t", "\n"])
    def test_empty_or_whitespace_rejected(self, name):
        with pytest.raises(InvalidPVName):
            validate_pv_name(name)

    def test_over_length_rejected(self):
        with pytest.raises(InvalidPVName):
            validate_pv_name("X" * (MAX_PV_NAME_LEN + 1))

    @pytest.mark.parametrize("name", ["EVIL\x00PV", "bad\x1fchar", "tail\x7f"])
    def test_control_characters_rejected(self, name):
        with pytest.raises(InvalidPVName):
            validate_pv_name(name)

    def test_residual_scheme_rejected(self):
        # A doubled / mistyped prefix leaves "://" in the stripped name.
        with pytest.raises(InvalidPVName):
            validate_pv_name("pva://ca://X")

    def test_non_string_rejected(self):
        with pytest.raises(InvalidPVName):
            validate_pv_name(123)  # type: ignore[arg-type]

    def test_invalid_pv_name_is_http_400(self):
        exc = InvalidPVName("nope")
        assert exc.status_code == 400
