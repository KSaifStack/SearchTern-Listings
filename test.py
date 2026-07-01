import duckdb
import readme
url = "https://storage.stapply.ai/jobhive/v1/all.parquet"
result = duckdb.sql(f"""
    SELECT 
        company,
        title as role,
        location,
        posted_at as date,
        url as link
    FROM (
        SELECT *,
            ROW_NUMBER() OVER (
                PARTITION BY company, title, location 
                ORDER BY posted_at DESC
            ) as rn
        FROM read_parquet('{url}')
        WHERE (commitment ILIKE '%intern%' OR title ILIKE '%intern%')
          AND CAST(posted_at AS TIMESTAMP) >= CURRENT_DATE - INTERVAL '60 days'
          AND url IS NOT NULL
          AND title IS NOT NULL
          AND company IS NOT NULL
    )
    WHERE rn = 1
""").df()

readme.generate_readme(result,"README.md")

total = len(result)
missing_company = result["company"].isna().sum()
na_company = (result["company"] == "N/A").sum()
url_as_company = result["company"].str.contains(r'careers\.|\.com|\.org', na=False).sum()
# Check for empty strings specifically
empty_strings = (result["company"] == "").sum()
whitespace = result["company"].str.strip().eq("").sum()
print(f"Empty strings: {empty_strings}")
print(f"Whitespace only: {whitespace}")