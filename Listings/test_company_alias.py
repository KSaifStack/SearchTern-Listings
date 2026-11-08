"""Employer joining: legal suffixes stripped, measured initialisms aliased, nothing merged."""

import closure
import company_alias


def test_legal_suffix_is_not_part_of_the_name():
    assert company_alias.normalize("Northrop Grumman Corporation") == company_alias.normalize(
        "Northrop Grumman")
    assert company_alias.normalize("Acme, Inc.") == "acme"
    assert company_alias.normalize("Acme LLC") == "acme"


def test_measured_initialisms_join():
    assert company_alias.normalize("bah") == company_alias.normalize("Booz Allen")
    assert company_alias.normalize("Micron") == company_alias.normalize("Micron Technology")
    assert company_alias.normalize("llnl") == company_alias.normalize(
        "Lawrence Livermore National Laboratory")


def test_distinct_employers_are_never_merged():
    # A wrong merge lets one employer's board vote on another's postings.
    assert company_alias.normalize("General Dynamics") != company_alias.normalize(
        "General Dynamics Mission Systems")
    assert company_alias.normalize("Acme Labs") != company_alias.normalize("Acme")
    assert company_alias.normalize("Shield AI") == company_alias.normalize("Shield Ai")


def test_closure_joins_through_the_alias():
    assert closure.employer_key("Booz Allen") == closure.employer_key("bah")
    assert closure.employer_key("") == ""
    assert closure.employer_key(None) == ""
