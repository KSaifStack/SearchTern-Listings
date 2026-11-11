"""Regression checks for the career taxonomy and eligibility split.

Run: pytest -q test_taxonomy.py
"""

import classify
import taxonomy

# (title, expected family) — one legitimate early-career fixture per industry.
FAMILY_FIXTURES = [
    ("Software Engineer Intern", "Software Engineering & Technology"),
    ("Data Science Intern", "Software Engineering & Technology"),
    ("Security Analyst Intern", "Software Engineering & Technology"),
    ("Mechanical Engineering Intern",
     "Engineering (Mechanical, Electrical, Civil, Industrial, Aerospace)"),
    ("Civil Engineering Co-op",
     "Engineering (Mechanical, Electrical, Civil, Industrial, Aerospace)"),
    ("Investment Banking Summer Analyst", "Finance, Accounting & Banking"),
    ("Tax Intern", "Finance, Accounting & Banking"),
    ("Consulting Intern", "Business Analysis, Consulting & Administration"),
    ("Operations Intern", "Operations & Project Management"),
    ("Project Management Intern", "Operations & Project Management"),
    ("Supply Chain Intern", "Supply Chain, Procurement & Logistics"),
    ("Marketing Intern", "Marketing, Communications & PR"),
    ("Public Relations Intern", "Marketing, Communications & PR"),
    ("Human Resources Intern", "Human Resources & Recruiting"),
    ("Recruiting Coordinator Intern", "Human Resources & Recruiting"),
    ("Nursing Intern", "Healthcare, Clinical & Public Health"),
    ("Public Health Intern", "Healthcare, Clinical & Public Health"),
    ("Research Assistant", "Scientific Research, Lab & Biotech"),
    ("Biology Lab Intern", "Scientific Research, Lab & Biotech"),
    ("UX Design Intern", "Design, UX/UI & Creative"),
    ("Graphic Design Intern", "Design, UX/UI & Creative"),
    ("Teaching Assistant", "Education & Teaching"),
    ("Tutor", "Education & Teaching"),
    ("Policy Intern, City of Austin", "Government & Public Service"),
    ("Legal Intern", "Legal & Compliance"),
    ("Compliance Intern", "Legal & Compliance"),
    ("Sales Intern", "Sales & Customer Success"),
    ("Customer Success Intern", "Sales & Customer Success"),
]

# Titles that must never be refused because of their industry.
ELIGIBLE_FIXTURES = [
    "Nursing Intern", "Pharmacy Intern", "Teaching Assistant",
    "Marketing Intern", "Human Resources Intern", "Dental Assistant Intern",
    "Veterinary Technician Intern", "Sales Associate Intern",
    "Mechanical Engineering Intern", "Finance Intern", "Legal Intern",
    "Social Work Intern", "Agriculture Intern",
]


def test_every_family_has_a_fixture_and_is_classified():
    seen = {family for _title, family in FAMILY_FIXTURES}
    for title, expected in FAMILY_FIXTURES:
        result = taxonomy.classify(title)
        assert result["career_family"] == expected, (title, result)
        assert result["industry"]
        assert result["confidence"] in ("high", "medium")
        assert result["reason"]
    # The requirement lists sixteen families; fixtures cover all but the
    # fallback, which has its own test below.
    assert seen | {taxonomy.FALLBACK_FAMILY} == set(taxonomy.CAREER_FAMILIES)


def test_no_industry_vocabulary_is_in_the_eligibility_filter():
    for title in ELIGIBLE_FIXTURES:
        eligible, reason = taxonomy.opportunity_eligibility(title, "internship")
        assert eligible, (title, reason)


def test_eligibility_is_independent_of_family():
    finance = taxonomy.classify("Finance Intern")
    software = taxonomy.classify("Software Engineer Intern")
    assert finance["career_family"] != software["career_family"]
    assert taxonomy.opportunity_eligibility("Finance Intern", "internship")[0]
    assert taxonomy.opportunity_eligibility("Software Engineer Intern", "internship")[0]


def test_senior_titles_are_refused_but_senior_interns_are_not():
    assert taxonomy.opportunity_eligibility("Senior Software Engineer", "new_grad") == (
        False, "seniority_mismatch")
    assert taxonomy.opportunity_eligibility("Senior Software Engineer Intern", "internship")[0]
    assert taxonomy.opportunity_eligibility("Director of Marketing", "") == (
        False, "not_early_career")


def test_non_career_title_artifacts_are_refused():
    assert taxonomy.opportunity_eligibility("Internal Audit Intern", "internship") == (
        False, "non_career_title")
    assert taxonomy.opportunity_eligibility("Internal Medicine Intern", "internship") == (
        False, "non_career_title")


def test_unknown_title_falls_back_instead_of_dropping():
    result = taxonomy.classify("Zookeeper Intern")
    assert result["career_family"] == taxonomy.FALLBACK_FAMILY
    assert result["confidence"] == "low"
    assert result["reason"] == "no_keyword_match"
    assert result["career_families"] == [taxonomy.FALLBACK_FAMILY]


def test_cross_category_titles_keep_both_families():
    result = taxonomy.classify("Sales Engineer Intern")
    assert result["career_family"] == "Sales & Customer Success"
    assert "Software Engineering & Technology" in result["career_families"]
    assert len(result["career_families"]) <= 2


def test_every_family_and_industry_is_declared():
    for result in (taxonomy.classify(title) for title, _ in FAMILY_FIXTURES):
        assert result["career_family"] in taxonomy.CAREER_FAMILIES
        assert result["industry"] in taxonomy.INDUSTRIES


def test_legacy_category_is_still_produced():
    # Existing consumers read `category`; the taxonomy keeps filling it with the
    # same subcategory vocabulary.
    for title, _family in FAMILY_FIXTURES:
        result = taxonomy.classify(title)
        assert result["subcategory"] in classify.ROLE_CATEGORIES
