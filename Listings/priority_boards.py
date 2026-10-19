"""Priority ATS boards for the high-frequency "priority" tier.

These are companies whose public ATS APIs (Greenhouse / Lever / Ashby) are
polled on *every* run with no cooldown, so their newest postings surface in
near-real-time instead of waiting behind the 24h ATS-probe cooldown used by
the heavy tier.

Every (company, ats, slug) triple below was verified live: the endpoint
returned HTTP 200 with at least one posting. Keep this list curated and
verified — a wrong slug silently yields nothing and just wastes a fetch.
"""

PRIORITY_BOARDS = [
    # --- Greenhouse -------------------------------------------------------
    ("Affirm", "Greenhouse", "affirm"),
    ("Airbnb", "Greenhouse", "airbnb"),
    ("Airtable", "Greenhouse", "airtable"),
    ("Anthropic", "Greenhouse", "anthropic"),
    ("Asana", "Greenhouse", "asana"),
    ("Brex", "Greenhouse", "brex"),
    ("Chime", "Greenhouse", "chime"),
    ("Cloudflare", "Greenhouse", "cloudflare"),
    ("Coinbase", "Greenhouse", "coinbase"),
    ("Databricks", "Greenhouse", "databricks"),
    ("Datadog", "Greenhouse", "datadog"),
    ("Discord", "Greenhouse", "discord"),
    ("Dropbox", "Greenhouse", "dropbox"),
    ("Duolingo", "Greenhouse", "duolingo"),
    ("Figma", "Greenhouse", "figma"),
    ("Flexport", "Greenhouse", "flexport"),
    ("Ginkgo Bioworks", "Greenhouse", "ginkgobioworks"),
    ("GitLab", "Greenhouse", "gitlab"),
    ("Gusto", "Greenhouse", "gusto"),
    ("Instacart", "Greenhouse", "instacart"),
    ("Lyft", "Greenhouse", "lyft"),
    ("MongoDB", "Greenhouse", "mongodb"),
    ("Pinterest", "Greenhouse", "pinterest"),
    ("Reddit", "Greenhouse", "reddit"),
    ("Robinhood", "Greenhouse", "robinhood"),
    ("Samsara", "Greenhouse", "samsara"),
    ("Scale AI", "Greenhouse", "scaleai"),
    ("Stripe", "Greenhouse", "stripe"),
    ("Twilio", "Greenhouse", "twilio"),
    ("Waymo", "Greenhouse", "waymo"),
    # --- Lever ------------------------------------------------------------
    ("Palantir", "Lever", "palantir"),
    ("Spotify", "Lever", "spotify"),
    ("Wealthfront", "Lever", "wealthfront"),
    # --- Ashby ------------------------------------------------------------
    ("Astronomer", "Ashby", "astronomer"),
    ("Baseten", "Ashby", "baseten"),
    ("Cohere", "Ashby", "cohere"),
    ("Cursor", "Ashby", "cursor"),
    ("ElevenLabs", "Ashby", "elevenlabs"),
    ("Harvey", "Ashby", "harvey"),
    ("LangChain", "Ashby", "langchain"),
    ("Linear", "Ashby", "linear"),
    ("Modal", "Ashby", "modal"),
    ("Mux", "Ashby", "mux"),
    ("Notion", "Ashby", "notion"),
    ("OpenAI", "Ashby", "openai"),
    ("Perplexity", "Ashby", "perplexity"),
    ("Pinecone", "Ashby", "pinecone"),
    ("PostHog", "Ashby", "posthog"),
    ("Ramp", "Ashby", "ramp"),
    ("Replit", "Ashby", "replit"),
    ("Runway", "Ashby", "runway"),
    ("Sardine", "Ashby", "sardine"),
    ("Sierra", "Ashby", "sierra"),
    ("Suno", "Ashby", "suno"),
    ("Supabase", "Ashby", "supabase"),
    ("Vanta", "Ashby", "vanta"),
]


def boards():
    """Return the priority board triples (copies, so callers can mutate)."""
    return list(PRIORITY_BOARDS)
