from readme_utils import canonical_url

DEDUP_SUBSET = ["company", "role", "location"]
# A requisition id is the posting's identity; the URL is only its address. Same
# job reached through a tracker link and the employer's ATS must collapse, so
# (ats, id) wins over URL when both are known.
ID_COLUMNS = ("ats_type", "ats_id")


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
    keys = ("url|" + links).where(links.ne(""), fallback)
    if all(column in df.columns for column in ID_COLUMNS):
        ats = df["ats_type"].astype(str).str.strip().str.lower()
        req = df["ats_id"].astype(str).str.strip()
        identified = req.ne("") & req.ne("nan") & req.ne("none") & ats.ne("")
        keys = ("id|" + ats + "|" + req).where(identified, keys)
    return keys


def dedupe(df):
    if df.empty:
        return df
    return df[~dedup_keys(df).duplicated(keep="first")].copy()


def dedup_across(df_a, df_b):
    if df_a.empty or df_b.empty:
        return df_b
    return df_b[~dedup_keys(df_b).isin(set(dedup_keys(df_a)))].copy()
