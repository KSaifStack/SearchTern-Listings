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


def test_every_alias_target_is_reachable():
    """An alias is only useful if a real spelling normalizes to its target.

    Audit 2026-11-08 found two that did not: ``snc`` pointed at
    ``sierranevadacorporation`` and ``aero`` at ``aerospacecorporation``, but
    suffix stripping turns "Sierra Nevada Corporation" into ``sierranevada``,
    so neither alias could ever join anything.
    """
    for key, target in company_alias._ALIASES.items():
        assert company_alias.normalize(target) == target, (key, target)


def test_similar_names_stay_apart():
    """A short key must not swallow a neighbouring employer.

    ``snc`` is aliased to Sierra Nevada, so SNC-Lavalin spelled out has to
    survive as its own key; ``talos`` is aliased to Talos Trading, so Talos
    Energy has to survive too.
    """
    assert company_alias.normalize("SNC-Lavalin") != company_alias.normalize(
        "Sierra Nevada Corporation")
    assert company_alias.normalize("Talos Energy") != company_alias.normalize(
        "Talos Trading")


def test_alliance_tenant_is_nissan():
    """Verified 2026-11-08: the two "Alliance" rows apply on
    alliance.wd3.myworkdayjobs.com/NissanPrivateExternal, so the alias joins
    Nissan's own board rather than a stranger's."""
    assert company_alias.normalize("Alliance") == company_alias.normalize("Nissan")


def test_closure_joins_through_the_alias():
    assert closure.employer_key("Booz Allen") == closure.employer_key("bah")
    assert closure.employer_key("") == ""
    assert closure.employer_key(None) == ""
