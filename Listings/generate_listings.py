import requests
import duckdb
import pandas as pd
import readme_generation

ALLOWED_ATS = [
    'greenhouse', 'lever', 'ashby', 'workday', 'icims',
    'bamboohr', 'rippling', 'smartrecruiters', 'teamtailor',
    'recruitee', 'breezy', 'pinpoint', 'workable',
    'successfactors', 'phenom', 'avature', 'cornerstone',
    'eightfold', 'gem', 'recruiterbox', 'personio',
    'amazon', 'apple', 'tesla', 'google', 'tiktok', 'uber',
    'ycombinator',
]

# --- Title exclusions (shared base + per-pipeline extras) ---
COMMON_TITLE_EXCLUSIONS = [
    'pharmacist', 'pharmacy', 'dental', 'nurse', 'nursing', 'physician',
    'medical intern', 'clinical intern', 'internal medicine', 'internal audit',
    'internal only', 'internal security', 'sales associate',
    'sales representative', 'veterinary', 'pastor', 'teacher',
]
README_ONLY_EXCLUSIONS = ['data entry', 'front end entry', 'international only']
LISTINGS_ONLY_EXCLUSIONS = ['marketing intern', 'hr intern', 'human resources']

def _exclusion_clause(terms):
    return "".join(f"\n    AND title NOT ILIKE '%{t}%'" for t in terms)

README_TITLE_EXCLUSIONS = _exclusion_clause(COMMON_TITLE_EXCLUSIONS + README_ONLY_EXCLUSIONS)
LISTINGS_TITLE_EXCLUSIONS = _exclusion_clause(COMMON_TITLE_EXCLUSIONS + LISTINGS_ONLY_EXCLUSIONS)

# --- Intern / new-grad matching conditions (used once per query, in both
#     the CASE and the WHERE, instead of being retyped in each place) ---
README_INTERN_COND = """
            commitment ILIKE '%intern%'
            OR title ILIKE '%intern%'
            OR title ILIKE '%co-op%'
            OR title ILIKE '%coop%'
            OR title ILIKE '%undergraduate research%'
            OR title ILIKE '%undergrad research%'
            OR (
                title ILIKE '%research assistant%'
                AND title NOT ILIKE '%postdoc%'
                AND title NOT ILIKE '%post-doc%'
                AND title NOT ILIKE '%phd%'
            )
            OR title ILIKE '%student researcher%'
            OR title ILIKE '%student research%'
            OR title ILIKE '%REU%'
            OR title ILIKE '%summer research%'
            OR title ILIKE '%undergraduate assistant%'
"""

README_NEWGRAD_COND = """
            title ILIKE '%new grad%'
            OR title ILIKE '%new graduate%'
            OR title ILIKE '%entry level%'
            OR title ILIKE '%entry-level%'
            OR title ILIKE '%early career%'
            OR title ILIKE '%campus%'
            OR title ILIKE '%rotational%'
            OR title ILIKE '%graduate engineer%'
            OR title ILIKE '%graduate developer%'
            OR title ILIKE '%graduate analyst%'
            OR commitment ILIKE '%new grad%'
            OR commitment ILIKE '%entry%'
"""

LISTINGS_INTERN_COND = """
            commitment ILIKE '%intern%' OR title ILIKE '%intern%' OR title ILIKE '%co-op%'
            OR title ILIKE '%coop%' OR title ILIKE '%undergraduate%' OR title ILIKE '%undergrad%'
            OR title ILIKE '%student%' OR title ILIKE '%REU%' OR title ILIKE '%apprentice%'
            OR commitment ILIKE '%student%'
"""

LISTINGS_NEWGRAD_COND = """
            title ILIKE '%new grad%' OR title ILIKE '%new graduate%'
            OR title ILIKE '%university grad%' OR title ILIKE '%university graduate%'
            OR title ILIKE '%entry level%' OR title ILIKE '%entry-level%'
            OR title ILIKE '%early career%' OR title ILIKE '%campus%'
            OR title ILIKE '%rotational%' OR commitment ILIKE '%new grad%'
            OR commitment ILIKE '%entry%'
"""

COUNTRY_EXCLUSIONS = """
        AND country_iso NOT IN (
            'DE', 'AT', 'CH', 'FR', 'PL', 'NO', 'SE', 'DK',
            'NL', 'IT', 'ES', 'PT', 'RO', 'HU', 'CZ', 'SK',
            'HR', 'BG', 'FI', 'LU', 'BE', 'MT', 'CY'
        )"""


def build_job_query(intern_cond, newgrad_cond, title_exclusions, lookback_days, extra_where=""):
    """Shared skeleton for both the README and listings.json pipelines.
    Only the match conditions, exclusions, lookback window, and any extra
    WHERE clause differ between callers."""
    return f"""
    SELECT
        company,
        title        as role,
        location,
        posted_at    as date,
        url          as link,
        is_remote,
        salary_min,
        salary_max,
        salary_currency,
        country_iso,
        CASE
            WHEN ({intern_cond}){title_exclusions}
            AND url IS NOT NULL
            AND TRIM(url) != ''
            AND location IS NOT NULL
            THEN 'internship'

            WHEN ({newgrad_cond}){title_exclusions}
            AND url IS NOT NULL
            AND TRIM(url) != ''
            AND location IS NOT NULL
            THEN 'new_grad'

            ELSE 'other'
        END as job_type

    FROM (
        SELECT *,
            ROW_NUMBER() OVER (
                PARTITION BY company, title, location
                ORDER BY posted_at DESC
            ) as rn
        FROM read_parquet($1)
        WHERE ({intern_cond} OR {newgrad_cond})
        AND CAST(posted_at AS TIMESTAMP) >= CURRENT_DATE - INTERVAL '{lookback_days} days'
        AND url IS NOT NULL AND title IS NOT NULL AND company IS NOT NULL
        AND title NOT ILIKE '%(m/w/d)%'
        AND title NOT ILIKE '%(m/f/d)%'
        AND title NOT ILIKE '%(w/m/d)%'{extra_where}
    )
    WHERE rn = 1
    ORDER BY posted_at DESC
"""


manifest = requests.get("https://storage.stapply.ai/jobhive/v1/manifest.json").json()
parquet_urls = [
    manifest["by_ats"][ats]["parquet"]
    for ats in ALLOWED_ATS
    if ats in manifest["by_ats"]
]

# --- README pipeline (pasted code filters) ---
# Country exclusions, ASCII-only titles, 60-day lookback, narrow patterns
readme_query = build_job_query(
    README_INTERN_COND, README_NEWGRAD_COND, README_TITLE_EXCLUSIONS,
    lookback_days=60,
    extra_where=f"""
        AND title ~ '^[[:ascii:]]+$'{COUNTRY_EXCLUSIONS}""",
)
readme_result = duckdb.execute(readme_query, [parquet_urls]).df()
readme_result = readme_result[readme_result["job_type"] != "other"]

# --- Listings.json pipeline (current generate_listings.py filters) ---
# Broader patterns, 90-day lookback, tech keyword filter, no country exclusions
listings_query = build_job_query(
    LISTINGS_INTERN_COND, LISTINGS_NEWGRAD_COND, LISTINGS_TITLE_EXCLUSIONS,
    lookback_days=90,
)
listings_result = duckdb.execute(listings_query, [parquet_urls]).df()
listings_result = listings_result[listings_result["job_type"] != "other"]

_TECH_KEYWORDS = (
    r"\b(swe|sde|mts|it)\b", "software", "developer", "programmer", "coder", "engineer",
    "data", "machine learn", "deep learn", "artificial intellig", " ai ", "ai/", "ml",
    "nlp", "computer vision", "cloud", "devops", "sre", "site reliabil", "cybersecur",
    "security", "full stack", "fullstack", "full-stack", "backend", "back-end",
    "frontend", "front end", "front-end", "web", "mobile", "ios", "android", "qa",
    "quality assur", "test", "sdet", "systems", "network", "sysadmin", "infrastructure",
    "platform", "hardware", "firmware", "embedded", "fpga", "asic", "chip", "database",
    "dba", "ux", "ui", "design", "product", "quant", "computer science", "linux",
    "unix", "robotics", "compiler", "distributed", "game", "blockchain", "web3",
    "cryptograph", "hpc", "scientific comput", "technical", "technology",
    "research", "electrical", "electronics", "controls", "signal", "telecom",
    "architect", "devrel", "automation", "applied scien", "analyst"
)

role_lower = listings_result["role"].str.lower()
tech_mask = role_lower.str.contains('|'.join(_TECH_KEYWORDS), regex=True, na=False)
listings_result = listings_result[tech_mask]

# --- Output Pipelines ---
readme_generation.generate_readme(readme_result, output_dir="..")
readme_generation.write_listings_json(listings_result, output_dir="..")

# --- Metrics ---
print("\n--- README stats ---")
r_total = len(readme_result)
r_internships = (readme_result["job_type"] == "internship").sum()
r_new_grads = (readme_result["job_type"] == "new_grad").sum()
print(f"Total listings  : {r_total:,}")
print(f"  Internships   : {int(r_internships):,}")
print(f"  New grad      : {int(r_new_grads):,}")

print("\n--- Listings.json stats ---")
l_total = len(listings_result)
l_internships = (listings_result["job_type"] == "internship").sum()
l_new_grads = (listings_result["job_type"] == "new_grad").sum()
l_remote = (listings_result["is_remote"].astype(str).str.lower() == "true").sum()
numeric_salary = pd.to_numeric(listings_result["salary_min"], errors='coerce')
l_paid = (numeric_salary > 0).sum()
print(f"Total listings  : {l_total:,}")
print(f"  Internships   : {int(l_internships):,}")
print(f"  New grad      : {int(l_new_grads):,}")
print(f"Remote roles    : {int(l_remote):,}")
print(f"Paid roles      : {int(l_paid):,}")