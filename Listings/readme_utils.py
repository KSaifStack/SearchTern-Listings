import html
import re
from datetime import datetime, timezone

GITHUB_FILE_SIZE_LIMIT = 450_000
SIZE_BUFFER = 8_000

INACTIVE_THRESHOLD_DAYS = 60

# Base GitHub URL for nav links
BASE_URL = "https://github.com/KSaifStack/jobscraper/blob/main/"

APPLY_BUTTON = "https://i.imgur.com/fbjwDvo.png"

MAX_COMPANY_LEN = 20
MAX_ROLE_LEN = 48

BLOCKED_COMPANIES: set[str] = {
    "aoglobelife",
    "aoglobelifebrittmarieyarian",
    "aoglobelifesholaadebayo",
    "americanincomelif",
    "aoglobelifebrittm",
    "destinationknot",
    "themcquadeorganiz",
    "globelifeaillisar",
}
NORMALIZED_BLOCKED_COMPANIES = {name.strip().lower() for name in BLOCKED_COMPANIES}


def is_oracle_cloud_url(company: str) -> bool:
    """Detect Oracle Cloud ATS URLs masquerading as company names."""
    normalized = company.strip().lower()
    return (
        ".fa." in normalized and "oracl" in normalized
        or normalized.endswith(".oraclecloud.com")
        or "fa-" in normalized and "saasfaprod" in normalized
    )


def is_careers_portal_url(company: str) -> bool:
    """Detect careers portal URLs masquerading as company names."""
    normalized = company.strip().lower()
    return "careers." in normalized and re.search(r"\.\w{2,}(?:\.\w{2,})?$", normalized) is not None


FAANG_PLUS: set[str] = {
    "airbnb", "adobe", "amazon", "amd", "anthropic", "apple",
    "asana", "atlassian", "bytedance", "cloudflare", "coinbase",
    "crowdstrike", "databricks", "datadog", "doordash", "dropbox",
    "duolingo", "figma", "google", "ibm", "instacart", "intel",
    "linkedin", "lyft", "meta", "microsoft", "netflix", "notion",
    "nvidia", "openai", "oracle", "palantir", "paypal", "perplexity",
    "pinterest", "ramp", "reddit", "rippling", "robinhood", "roblox",
    "salesforce", "samsara", "servicenow", "shopify", "slack", "snap",
    "snapchat", "spacex", "splunk", "snowflake", "stripe", "square",
    "tesla", "tinder", "tiktok", "uber", "visa", "waymo", "x",
}

COUNTRY_NAMES = {
    "US": "🇺🇸 United States",
    "CA": "🇨🇦 Canada",
    "GB": "🇬🇧 United Kingdom",
    "AU": "🇦🇺 Australia",
    "NZ": "🇳🇿 New Zealand",
    "IE": "🇮🇪 Ireland",
    "SG": "🇸🇬 Singapore",
    "IN": "🇮🇳 India",
    "DE": "🇩🇪 Germany",
    "NL": "🇳🇱 Netherlands",
    "FR": "🇫🇷 France",
    "JP": "🇯🇵 Japan",
    "BR": "🇧🇷 Brazil",
    "MX": "🇲🇽 Mexico",
    "CH": "🇨🇭 Switzerland",
    "AT": "🇦🇹 Austria",
    "BE": "🇧🇪 Belgium",
    "SE": "🇸🇪 Sweden",
    "ES": "🇪🇸 Spain",
    "LU": "🇱🇺 Luxembourg",
    "IT": "🇮🇹 Italy",
    "PL": "🇵🇱 Poland",
    "NO": "🇳🇴 Norway",
    "DK": "🇩🇰 Denmark",
    "FI": "🇫🇮 Finland",
    "MT": "🇲🇹 Malta",
    "PT": "🇵🇹 Portugal",
    "CZ": "🇨🇿 Czech Republic",
    "CY": "🇨🇾 Cyprus",
    "RO": "🇷🇴 Romania",
}

CANONICAL_DOMAIN_MAP: dict[str, str] = {
    "baesystems": "BAE Systems",
    "marsh": "Marsh",
    "cencora": "Cencora",
    "geaerospace": "GE Aerospace",
    "ascension": "Ascension",
}


def clean_company_name(company: str) -> str:
    """Normalize company values that are domains or noisy tokens."""
    if not company:
        return ""
    name = html.unescape(str(company)).strip()
    if not name:
        return ""

    lower = name.lower()

    if is_oracle_cloud_url(lower):
        return "Oracle Cloud"

    if is_careers_portal_url(lower):
        m = re.search(r"careers\.([a-z0-9-]+)", lower)
        if m:
            sld = m.group(1)
            if sld in CANONICAL_DOMAIN_MAP:
                return CANONICAL_DOMAIN_MAP[sld]
            s = re.sub(r"[-_0-9]+", " ", sld).strip()
            return " ".join(w.capitalize() for w in s.split())

    if "." in lower and " " not in lower:
        m = re.search(r"(?:^(?:www|jobs|careers|apply)\.)?([a-z0-9-]+)(?:\.[a-z0-9-]+)+$", lower)
        if m:
            sld = m.group(1)
            if sld in CANONICAL_DOMAIN_MAP:
                return CANONICAL_DOMAIN_MAP[sld]
            s = re.sub(r"[-_0-9]+", " ", sld).strip()
            return " ".join(w.capitalize() for w in s.split())

    cleaned = re.sub(r"(?:careers|jobs|portal|apply|recruitment|ats|jobsat)$", "", lower)
    cleaned = re.sub(r"\d+$", "", cleaned)
    cleaned = re.sub(r"[^a-z0-9]+", " ", cleaned).strip()
    if not cleaned:
        return ""

    if cleaned.isupper() or (len(cleaned) <= 4 and cleaned.isalpha()):
        return cleaned.upper()

    return " ".join(w.capitalize() for w in cleaned.split())


def is_english(text: str) -> bool:
    """Return False if text contains non-ASCII characters (catches foreign listings)."""
    try:
        text.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def truncate(text: str, max_len: int) -> str:
    """Truncate text to max_len characters."""
    text = text.strip()
    if len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


def normalize_company(company: str) -> str:
    """Normalize company names so blocking works consistently."""
    return str(company).strip().lower()


def days_display(date_str) -> str:
    """Convert ISO timestamp to human-readable age string."""
    try:
        posted = datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        days = max((datetime.now(timezone.utc) - posted).days, 0)
        if days == 0:
            return "Today"
        if days > 30:
            return f"{days // 30}mo"
        return f"{days}d"
    except Exception:
        return str(date_str)


def format_location(location) -> str:
    """Format location — collapse 4+ locations into a dropdown."""
    if not location or str(location) == "nan":
        return "N/A"
    
    parts = []
    for p in str(location).split(","):
        cleaned = p.strip()
        if not cleaned:
            continue
        if cleaned.upper() == "UNAVAILABLE":
            cleaned = "N/A"
        escaped = html.escape(cleaned)
        if escaped not in parts:
            parts.append(escaped)
            
    if not parts or parts == ["N/A"]:
        return "N/A"
        
    if "N/A" in parts and len(parts) > 1:
        parts.remove("N/A")

    if len(parts) <= 3:
        return "<br>".join(parts)
    joined = "<br>".join(parts)
    return f"<details><summary><strong>{len(parts)} locations</strong></summary>{joined}</details>"


def format_company(company: str, prev_company: str, age: str, prev_age: str, link: str | None = None) -> str:
    """Return company HTML. Wraps in anchor when a job URL is provided."""
    escaped = html.escape(company)
    if company.lower() in FAANG_PLUS:
        base = f"🔥 <strong>{escaped}</strong>"
    else:
        base = f"<strong>{escaped}</strong>"

    if company == prev_company and age == prev_age:
        cell = f"↳ {base}"
    else:
        cell = base

    if link:
        safe_link = html.escape(link, quote=True)
        return f'<a href="{safe_link}">{cell}</a>'
    return cell


def build_table(rows: list[str]) -> str:
    """Wrap rows in a full HTML table and keep it within GitHub's viewport width."""
    return (
        '<table style="width:100%; table-layout:fixed; border-collapse:collapse;">\n'
        '<colgroup>\n'
        '<col style="width:14%">\n'
        '<col style="width:46%">\n'
        '<col style="width:24%">\n'
        '<col style="width:8%">\n'
        '<col style="width:8%">\n'
        '</colgroup>\n<thead>\n<tr>\n'
        '<th style="text-align:left; white-space:normal;">Company</th>\n'
        '<th style="text-align:left; white-space:normal;">Role</th>\n'
        '<th style="text-align:left; white-space:normal;">Location</th>\n'
        '<th style="text-align:center; white-space:normal;">Application</th>\n'
        '<th style="text-align:center; white-space:normal;">Age</th>\n'
        "</tr>\n</thead>\n<tbody>\n"
        + "\n".join(rows)
        + "\n</tbody>\n</table>"
    )
