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