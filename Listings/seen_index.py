"""Diff a run's rows against ``cache/seen.json`` by requisition, not by wording.

The seen log used to key on ``bucket|company|role|location`` text. That is how a
posting was *written down*, not what it *is*: a repunctuated title counted as a
new posting and its predecessor as a closure. Measured on the 2026-11-07
production state, the text rule reported 13,789 new / 74,040 closed on a day
with 703 genuinely new links (docs/reverse-engineering/validation-2026-11-07.md).

Entries now carry ``ats``/``req`` -- the same ``(ats_type, ats_id)`` the feed
publishes -- and a row is known when either its text key or its requisition id
was seen before. When the text changes and the id does not, the requisition
keeps its original ``first`` date, the old key is retired (so it is never
reported as a closure), and nothing is counted as new.

A closure is reported once: the entry is stamped ``closed`` and skipped on later
runs. Entries written before ids existed (no ``ats``/``req``) keep the old
key-only rule until they age out of the 120-day cutoff, which is the one
transitional wave this migration costs.
"""


import re

_ROLE_NOISE = re.compile(r"[^a-z0-9]+")


def loose_pair(key):
    """``company|role`` of a text key, with the role reduced to its letters.

    The location is deliberately dropped: the same requisition is written with a
    different location string all the time ("Remote" vs "Remote in US"), and a
    closure that ignores it errs toward keeping a posting, which is the safe
    direction.
    """
    parts = key.split("|", 3)
    if len(parts) < 3:
        return None
    return parts[1] + "|" + _ROLE_NOISE.sub("", parts[2].casefold())


def ids_by_key(rid_by_key):
    """Map each live ``(ats, req)`` to the text key that currently carries it."""
    out = {}
    for key, (ats, req) in rid_by_key.items():
        if ats and req:
            out.setdefault((ats, req), key)
    return out


def _entry_ids(meta):
    return (str(meta.get("ats") or "").strip(), str(meta.get("req") or "").strip())


def seen_ids(seen):
    """``(ats, req) -> key`` for entries that carry an id and are not closed."""
    out = {}
    for key, meta in seen.items():
        rid = _entry_ids(meta)
        if rid[0] and rid[1] and not meta.get("closed"):
            out.setdefault(rid, key)
    return out


def new_keys(current_keys, rid_by_key, seen):
    """Keys that are neither seen text nor a seen requisition id."""
    known = seen_ids(seen)
    out = set()
    for key in current_keys:
        if key in seen:
            continue
        rid = rid_by_key.get(key, ("", ""))
        if rid[0] and rid[1] and rid in known:
            continue
        out.add(key)
    return out


def commit(seen, current_by_key, rid_by_key, fresh_buckets, bucket_of, obs,
           live_keys=None, live_pairs=None):
    """Fold this run into the seen log.

    Returns ``(closed, seen_out, first_by_key)``. ``closed`` lists keys whose
    bucket was re-fetched this run and whose requisition is gone; ``first_by_key``
    preserves the original first-seen date across a text-key change.

    ``live_keys`` is everything the run fetched, which is wider than the keys it
    published: a posting the feed filters out is still listed, so absence from
    the published subset is not a closure. Defaults to the published keys.

    ``live_pairs`` is the loose form of the same set (see ``loose_pair``) and
    catches the posting whose title or location was rewritten between runs --
    still listed, different text, no id to prove it.
    """
    current_keys = set(current_by_key)
    live = current_keys if live_keys is None else live_keys
    live_ids = ids_by_key(rid_by_key)

    closed = []
    for key, meta in seen.items():
        if meta.get("closed") or key in live:
            continue
        if live_pairs and loose_pair(key) in live_pairs:
            continue  # same company and role, rewritten location or title
        rid = _entry_ids(meta)
        if rid[0] and rid[1] and rid in live_ids:
            continue  # same requisition under a new text key
        bucket = bucket_of(key.split("|", 1)[0])
        if fresh_buckets.get(bucket, False):
            closed.append({
                "key": key,
                "first_seen": meta.get("first", ""),
                "last_seen": meta.get("last", ""),
                "ats_type": rid[0],
                "ats_id": rid[1],
            })
            meta["closed"] = obs

    prior_ids = seen_ids(seen)
    first_by_key = {}
    out = {}
    for key in current_by_key:
        rid = rid_by_key.get(key, ("", ""))
        prior = seen.get(key)
        if prior is None and rid in prior_ids:
            prior = seen.get(prior_ids[rid])
        prior = prior or {}
        out[key] = {"first": prior.get("first", obs), "last": obs,
                    "ats": rid[0], "req": rid[1], "closed": ""}
        first_by_key[key] = out[key]["first"]

    # Retire the old text key of a requisition that moved spelling: keeping both
    # would report it closed on the next run.
    for key, meta in seen.items():
        if key in current_keys:
            continue
        rid = _entry_ids(meta)
        if rid[0] and rid[1] and rid in live_ids:
            continue
        out[key] = meta
    return closed, out, first_by_key
