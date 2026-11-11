"""Display quality: employer names, identity keys, and job titles.

The feed is consumed two ways: the frontend reads the JSON (so ``company`` must
be recognizable and ``company_key`` stable), and the README tables are rendered
from the same frame (so titles must not overflow their column). These fixtures
pin the rules that keep both honest.
"""

import company_names
from readme_utils import clean_role


def test_reported_company_examples():
    assert company_names.normalize("Andurilindustries").display == "Anduril Industries"
    assert company_names.normalize("Columbiauniversity").display == "Columbia University"
    assert company_names.normalize("Sharkninjaoperatingllc").display == "SharkNinja"
    assert company_names.normalize("Morganmorganjobsapplynow").display == "Morgan & Morgan"


def test_verified_alias_carries_the_official_domain():
    assert company_names.normalize("Andurilindustries").domain == "anduril.com"
    assert company_names.normalize("Columbiauniversity").domain == "columbia.edu"
    # An employer with no verified domain still gets a canonical name and key.
    assert company_names.normalize("Sharkninjaoperatingllc").domain == "sharkninja.com"


def test_every_alias_target_is_a_fixed_point():
    """Re-normalizing a canonical name must return that name unchanged."""
    for raw_key, (display, _domain) in company_names._VERIFIED.items():
        resolved = company_names.normalize(display)
        assert resolved.display == display, (raw_key, display, resolved)
        assert resolved.key == company_names._key(display)


def test_aliases_share_one_identity_key():
    assert company_names.key("Andurilindustries") == company_names.key("Anduril Industries")
    assert company_names.key("Morganmorganjobsapplynow") == company_names.key("Morgan & Morgan")
    assert company_names.key("Sharkninjaoperatingllc") == company_names.key("SharkNinja")


def test_descriptor_split_is_not_word_segmentation():
    assert company_names.normalize("Syngentagroup").display == "Syngenta Group"
    assert company_names.normalize("Columbiauniversity").display == "Columbia University"
    # An unknown concatenation keeps the published spelling: no invented split.
    unknown = company_names.normalize("Gibberishbrandxyz")
    assert unknown.display == "Gibberishbrandxyz"
    assert unknown.confidence == "unchanged"
    # A word that merely contains a descriptor is untouched.
    assert company_names.normalize("Acme Labs").display == "Acme Labs"
    assert company_names.normalize("General Dynamics").display == "General Dynamics"


def test_spaced_names_are_never_stripped():
    assert company_names.normalize("Acme LLC").display == "Acme LLC"
    assert company_names.normalize("Acme, Inc.").display == "Acme, Inc."


def test_short_legal_suffixes_cannot_corrupt_a_name():
    """``SA`` is a legal form and also the end of ``USA`` (2026-11-11)."""
    assert company_names.normalize("Apogeeusa").display == "Apogee USA"
    assert company_names.normalize("Bgeinccampus").display == "BGE"


def test_distinct_employers_keep_distinct_keys():
    assert company_names.key("General Dynamics") != company_names.key(
        "General Dynamics Mission Systems")
    assert company_names.key("Acme Labs") != company_names.key("Acme")


def test_case_is_repaired_only_when_it_is_shouting():
    assert company_names.normalize("RAYTHEON TECHNOLOGIES").display == "Raytheon Technologies"
    assert company_names.normalize("iOS Developer Camp").display == "iOS Developer Camp"
    assert company_names.normalize("McKinsey & Company").display == "McKinsey & Company"


def test_records_carry_canonical_identity_and_raw_value():
    """The publication, not just the helper, carries the canonical fields."""
    import pandas as pd
    import readme_generation

    frame = pd.DataFrame([{
        "company": "Andurilindustries",
        "role": "  SOFTWARE   ENGINEER INTERN ",
        "location": "Seattle, WA",
        "date": "2026-11-01",
        "link": "https://boards.greenhouse.io/acme/jobs/123456",
    }])
    records = readme_generation._build_json_records(frame)
    assert len(records) == 1
    record = records[0]
    assert record["company"] == "Anduril Industries"
    assert record["company_key"] == "andurilindustries"
    assert record["company_domain"] == "anduril.com"
    assert record["company_raw"] == "Andurilindustries"
    assert record["role"] == "Software Engineer Intern"
    assert record["role_raw"] == "SOFTWARE   ENGINEER INTERN"


def test_title_whitespace_and_markup():
    assert clean_role("  SOFTWARE   ENGINEERING INTERN  ") == "Software Engineering Intern"
    assert clean_role("<b>Data</b> Intern &amp; Analyst") == "Data Intern & Analyst"
    assert clean_role("Intern -") == "Intern"
    assert clean_role("") == ""


def test_title_case_is_repaired_without_rewording():
    assert clean_role("ENTRY LEVEL HVAC TECHNICIAN") == "Entry Level HVAC Technician"
    assert clean_role("REGULATORY AFFAIRS INTERN") == "Regulatory Affairs Intern"
    assert clean_role("CO-OP | REMOTE") == "Co-op | Remote"


def test_long_title_is_clipped_with_a_full_tooltip():
    from readme_utils import MAX_ROLE_LEN, role_cell

    title = 'Intern "Data" & Analytics ' + "x" * 80
    assert len(title) > MAX_ROLE_LEN
    cell = role_cell(title)
    assert "title=" in cell
    assert "&quot;" in cell  # the tooltip attribute is escaped
    assert "..." in cell
    assert cell.count("<td") == 1

    short = role_cell("SWE Intern")
    assert "title=" not in short
    assert short.endswith("SWE Intern</td>")


def test_title_distinctions_survive():
    # Acronyms and already-correct mixed case are left alone.
    assert clean_role("SWE Intern") == "SWE Intern"
    assert clean_role("iOS Developer Intern") == "iOS Developer Intern"
    assert clean_role("Machine Learning Intern II") == "Machine Learning Intern II"
    # No token is added, removed or reordered.
    assert clean_role("Data Science Intern (Summer 2027)") == "Data Science Intern (Summer 2027)"
