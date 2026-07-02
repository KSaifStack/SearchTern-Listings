import duckdb
import readme

url = "https://storage.stapply.ai/jobhive/v1/all.parquet"

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
            WHEN commitment ILIKE '%intern%'
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
                AND company NOT ILIKE 'fa-%'
                AND company NOT ILIKE '%.fa.%'
                AND company NOT ILIKE '%saas%'
                AND company NOT ILIKE 'eluq%'

                AND title NOT ILIKE '%pharmacist%'
                AND title NOT ILIKE '%pharmacy%'
                AND title NOT ILIKE '%dental%'
                AND title NOT ILIKE '%nurse%'
                AND title NOT ILIKE '%nursing%'
                AND title NOT ILIKE '%medical intern%'
                AND title NOT ILIKE '%physician%'
                AND title NOT ILIKE '%clinical intern%'
                AND url IS NOT NULL
                AND TRIM(url) != ''
                AND location IS NOT NULL
            THEN 'internship'

            WHEN title ILIKE '%new grad%'
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
                AND company NOT ILIKE 'fa-%'
                AND company NOT ILIKE '%.fa.%'
                AND company NOT ILIKE '%saas%'
                AND company NOT ILIKE 'eluq%'

                AND title NOT ILIKE '%pharmacist%'
                AND title NOT ILIKE '%pharmacy%'
                AND title NOT ILIKE '%dental%'
                AND title NOT ILIKE '%nurse%'
                AND title NOT ILIKE '%nursing%'
                AND title NOT ILIKE '%medical intern%'
                AND title NOT ILIKE '%physician%'
                AND title NOT ILIKE '%clinical intern%'
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
        FROM read_parquet('{url}')
        WHERE (
            -- Internship signals
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

            -- New grad signals
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
        -- These filters apply to ALL rows, not just some
        AND CAST(posted_at AS TIMESTAMP) >= CURRENT_DATE - INTERVAL '60 days'
        AND url     IS NOT NULL
        AND title   IS NOT NULL
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
        AND (
            TRIM(department) = ''
            OR department IS NULL
            OR department ILIKE '%engineer%'
            OR department ILIKE '%software%'
            OR department ILIKE '%data%'
            OR department ILIKE '%product%'
            OR department ILIKE '%design%'
            OR department ILIKE '%research%'
            OR department ILIKE '%IT%'
            OR department ILIKE '%technology%'
            OR department ILIKE '%computer%'
            OR department ILIKE '%analytics%'
            OR department ILIKE '%science%'
        )
    )
    WHERE rn = 1
    ORDER BY posted_at DESC
""").df()

result = result[result["job_type"] != "other"]

readme.generate_readme(result, output_dir=".")

# Stats
total       = len(result)
internships = (result["job_type"] == "internship").sum()
new_grads   = (result["job_type"] == "new_grad").sum()
remote      = (result["is_remote"].astype(str).str.lower() == "true").sum()
paid        = result["salary_min"].notna().sum()
empty       = (result["company"] == "").sum()
whitespace  = result["company"].str.strip().eq("").sum()

print(f"Total listings  : {total:,}")
print(f"  Internships   : {int(internships):,}")
print(f"  New grad      : {int(new_grads):,}")
print(f"Remote roles    : {int(remote):,}")
print(f"Paid roles      : {int(paid):,}")
print(f"Empty strings   : {empty}")
print(f"Whitespace only : {whitespace}")