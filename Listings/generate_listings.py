import requests
import duckdb
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

TITLE_EXCLUSIONS = """
    AND title NOT ILIKE '%pharmacist%'
    AND title NOT ILIKE '%pharmacy%'
    AND title NOT ILIKE '%dental%'
    AND title NOT ILIKE '%nurse%'
    AND title NOT ILIKE '%nursing%'
    AND title NOT ILIKE '%physician%'
    AND title NOT ILIKE '%medical intern%'
    AND title NOT ILIKE '%clinical intern%'
    AND title NOT ILIKE '%internal medicine%'
    AND title NOT ILIKE '%internal audit%'
    AND title NOT ILIKE '%internal only%'
    AND title NOT ILIKE '%internal security%'
    AND title NOT ILIKE '%sales associate%'
    AND title NOT ILIKE '%sales representative%'
    AND title NOT ILIKE '%data entry%'
    AND title NOT ILIKE '%front end entry%'
    AND title NOT ILIKE '%veterinary%'
    AND title NOT ILIKE '%pastor%'
    AND title NOT ILIKE '%teacher%'
    AND title NOT ILIKE '%international only%'
"""

manifest = requests.get("https://storage.stapply.ai/jobhive/v1/manifest.json").json()

parquet_urls = [
    manifest["by_ats"][ats]["parquet"]
    for ats in ALLOWED_ATS
    if ats in manifest["by_ats"]
]

result = duckdb.sql(f"""
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
            WHEN (
                commitment ILIKE '%intern%'
                OR title ILIKE '%intern%'
                OR title ILIKE '%co-op%'
                OR title ILIKE '%coop%'
                OR title ILIKE '%undergraduate research%'
                OR title ILIKE '%undergrad research%'
                OR title ILIKE '%research assistant%'
                OR title ILIKE '%student researcher%'
                OR title ILIKE '%student research%'
                OR title ILIKE '%REU%'
                OR title ILIKE '%summer research%'
                OR title ILIKE '%undergraduate assistant%'
            )
            {TITLE_EXCLUSIONS}
            AND url IS NOT NULL
            AND TRIM(url) != ''
            AND location IS NOT NULL
            THEN 'internship'

            WHEN (
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
            )
            {TITLE_EXCLUSIONS}
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
        FROM read_parquet({parquet_urls})
        WHERE (
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
            OR title ILIKE '%new grad%'
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
        )
        AND CAST(posted_at AS TIMESTAMP) >= CURRENT_DATE - INTERVAL '60 days'
        AND url IS NOT NULL
        AND title IS NOT NULL
        AND company IS NOT NULL
        AND title ~ '^[[:ascii:]]+$'
        AND title NOT ILIKE '%(m/w/d)%'
        AND title NOT ILIKE '%(m/f/d)%'
        AND title NOT ILIKE '%(w/m/d)%'
        AND country_iso NOT IN (
            'DE', 'AT', 'CH', 'FR', 'PL', 'NO', 'SE', 'DK',
            'NL', 'IT', 'ES', 'PT', 'RO', 'HU', 'CZ', 'SK',
            'HR', 'BG', 'FI', 'LU', 'BE', 'MT', 'CY'
        )
        
    )
    WHERE rn = 1
    ORDER BY posted_at DESC
""").df()

result = result[result["job_type"] != "other"]

readme_generation.generate_readme(result, output_dir="..")
readme_generation.write_listings_json(result, output_dir="..")

total       = len(result)
internships = (result["job_type"] == "internship").sum()
new_grads   = (result["job_type"] == "new_grad").sum()
remote      = (result["is_remote"].astype(str).str.lower() == "true").sum()
paid        = result["salary_min"].notna().sum()

print(f"Total listings  : {total:,}")
print(f"  Internships   : {int(internships):,}")
print(f"  New grad      : {int(new_grads):,}")
print(f"Remote roles    : {int(remote):,}")
print(f"Paid roles      : {int(paid):,}")