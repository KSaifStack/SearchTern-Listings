import company_names
import identity
from readme_utils import canonical_url

DEDUP_SUBSET = ["company", "role", "location"]
# A requisition id is the posting's identity; the URL is only its address. Same
# job reached through a tracker link and the employer's ATS must collapse, so
# (ats, id) wins over URL when both are known. Sources that publish no id get
# one derived from their URL (identity.pairs), so the identity rule applies to
# every source instead of only jobhive.
ID_COLUMNS = ("ats_type", "ats_id")


def dedup_keys(df):
    # Employer spellings are an identity, not a string: "Andurilindustries" and
    # "Anduril Industries", or "Morganmorganjobsapplynow" and "Morgan & Morgan",
    # are the same employer reaching us through different sources. Keying on the
    # raw text let both copies into the feed (2026-11-11 audit).
    parts = [df["company"].map(company_names.key)] + [
        df[col]
        .astype(str)
        .str.replace(r"\s+", " ", regex=True)
        .str.strip()
        .str.casefold()
        for col in DEDUP_SUBSET[1:]
    ]
    fallback = "details|" + parts[0].astype(str).str.cat(parts[1:], sep="|")
    if "link" not in df.columns:
        return fallback
    links = df["link"].map(canonical_url)
    keys = ("url|" + links).where(links.ne(""), fallback)
    ats, req = identity.pairs(df)
    ats = ats.str.lower()
    identified = ats.ne("") & req.ne("") & req.str.lower().ne("nan")
    return ("id|" + ats + "|" + req).where(identified, keys)


def dedupe(df):
    if df.empty:
        return df
    return df[~dedup_keys(df).duplicated(keep="first")].copy()


def dedup_across(df_a, df_b):
    if df_a.empty or df_b.empty:
        return df_b
    return df_b[~dedup_keys(df_b).isin(set(dedup_keys(df_a)))].copy()
