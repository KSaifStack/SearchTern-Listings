from readme_utils import canonical_url

DEDUP_SUBSET = ["company", "role", "location"]


def dedup_keys(df):
    parts = [
        df[col]
        .astype(str)
        .str.replace(r"\s+", " ", regex=True)
        .str.strip()
        .str.casefold()
        for col in DEDUP_SUBSET
    ]
    fallback = "details|" + parts[0].str.cat(parts[1:], sep="|")
    if "link" not in df.columns:
        return fallback
    links = df["link"].map(canonical_url)
    return ("url|" + links).where(links.ne(""), fallback)


def dedupe(df):
    if df.empty:
        return df
    return df[~dedup_keys(df).duplicated(keep="first")].copy()


def dedup_across(df_a, df_b):
    if df_a.empty or df_b.empty:
        return df_b
    return df_b[~dedup_keys(df_b).isin(set(dedup_keys(df_a)))].copy()
