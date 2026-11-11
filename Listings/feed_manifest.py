"""Feed-manifest verification, generation coherence, and physical recovery.

`feed-manifest.json` is the authoritative description of the published
generation: row counts, byte sizes, hashes and the snapshot ledger that produced
it. `run_meta.json`, `health.json` and `source_stats.json` are written earlier
from candidate frames, so they are patched from the manifest as part of the same
atomic publication.

A generation is not just the two JSON feeds. The README pages and country pages
are rendered from the same run, so they carry the generation id in a hidden
marker and are preserved with the feed. `audit()` fails when any published
surface still describes a different generation, and `recover()` restores the
whole surface (feed, manifest, README, countries) from the last-known-good copy.
"""
import contextlib
import glob
import hashlib
import json
import os
import shutil
import tempfile
import time

from atomic_fs import write_pairs

MANIFEST_NAME = "feed-manifest.json"
PUBLISHED_ARTIFACTS = ("listings", "searchtern")
LKG_DIR = "last-known-good"
LKG_CURRENT = "current.json"
LKG_ARTIFACTS = (*[f"{name}.json" for name in PUBLISHED_ARTIFACTS], MANIFEST_NAME)
# The count/health artifacts and the snapshot ledger describe the generation too;
# recovering the feed without them would leave audit() reporting a mixed run.
LKG_EXTRA = ("run_meta.json", "health.json", "source_stats.json", "snapshots.json",
             "coverage.json")
LKG_KEEP = 2
# Files that make up the public generation, relative to the repository root.
SURFACE_PATTERNS = ("README.md", "pages/README-*.md", "pages/feed.xml", "countries/*.md")
GENERATION_MARKER = "<!-- searchtern:generation {generation} -->"
LEGACY_STATUS = "publication_status"


class ManifestMismatch(RuntimeError):
    """The published surface does not describe one coherent generation."""


def read(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def make_run_id(generated_at, digest):
    """Stable id for one publication: UTC second plus the feed's hash prefix."""
    try:
        base = generated_at.strftime("%Y%m%dT%H%M%SZ")
    except (AttributeError, ValueError):
        base = str(generated_at).split(".")[0]
        base = base.replace("-", "").replace(":", "")
        base = base.replace("+0000", "Z").replace("+00:00", "Z").replace(" ", "T")
    return f"{base}-{str(digest)[:12]}"


def marker(generation):
    return GENERATION_MARKER.format(generation=generation or "unknown")


def _root_of(pages_dir):
    return os.path.dirname(os.path.abspath(pages_dir))


def _lkg_root(pages_dir):
    return os.path.join(pages_dir, LKG_DIR)


def _current_path(pages_dir):
    return os.path.join(_lkg_root(pages_dir), LKG_CURRENT)


def _surface_files(pages_dir):
    """Every published file that belongs to the current generation."""
    root = _root_of(pages_dir)
    paths = [os.path.join(pages_dir, name) for name in (*LKG_ARTIFACTS, *LKG_EXTRA)]
    for pattern in SURFACE_PATTERNS:
        paths.extend(glob.glob(os.path.join(root, pattern)))
    return sorted(path for path in paths if os.path.isfile(path))


def _generation_pages(source):
    """The pages directory inside a generation (nested layout, or legacy flat)."""
    nested = os.path.join(source, "pages")
    if os.path.exists(os.path.join(nested, MANIFEST_NAME)):
        return nested
    return source


def _surface_rel(path, root):
    return os.path.relpath(os.path.abspath(path), root).replace(os.sep, "/")


def _is_surface_rel(rel, legacy=False):
    if rel in SURFACE_PATTERNS:
        return True
    if rel in (f"pages/{name}" for name in (*LKG_ARTIFACTS, *LKG_EXTRA)):
        return True
    parent, _slash, name = rel.rpartition("/")
    if parent == "pages" and name.startswith("README-") and name.endswith(".md"):
        return True
    if parent == "countries" and name.endswith(".md"):
        return True
    return legacy and rel in LKG_ARTIFACTS


def _legacy_dest(rel):
    """Map a legacy flat generation's core files back under `pages/`."""
    return f"pages/{rel}" if rel in LKG_ARTIFACTS else rel


def read_last_known_good(pages_dir):
    """The pointer to the newest physically preserved generation, if any."""
    return read(_current_path(pages_dir))


def has_last_known_good(pages_dir):
    pointer = read_last_known_good(pages_dir)
    generation = str((pointer or {}).get("generation") or "")
    return bool(generation) and os.path.isdir(os.path.join(_lkg_root(pages_dir), generation))


def _write_json(path, payload):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=directory, prefix=f".{os.path.basename(path)}.",
                                    suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _json_pairs(payloads):
    return [
        (path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        for path, payload in payloads
    ]


def _write_json_pairs(payloads):
    write_pairs(_json_pairs(payloads))


def json_pairs(payloads):
    """Public form of the JSON text pairs used by the publication transaction."""
    return _json_pairs(payloads)


def _write_bytes(path, data):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=directory, prefix=f".{os.path.basename(path)}.",
                                    suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _restore_pairs(pairs):
    """Replace several files together; restore every old file on failure."""
    staged = []
    backups = {}
    replaced = []
    try:
        for path, data in pairs:
            directory = os.path.dirname(os.path.abspath(path))
            os.makedirs(directory, exist_ok=True)
            handle, temp = tempfile.mkstemp(
                dir=directory, prefix=f".{os.path.basename(path)}.", suffix=".tmp"
            )
            with os.fdopen(handle, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            staged.append((path, temp))

        for path, _temp in staged:
            if os.path.exists(path):
                backup = f"{path}.bak.lkg.{os.getpid()}"
                os.replace(path, backup)
                backups[path] = backup

        for path, temp in staged:
            os.replace(temp, path)
            replaced.append(path)
        for backup in backups.values():
            with contextlib.suppress(OSError):
                os.unlink(backup)
    except Exception:
        for path in replaced:
            with contextlib.suppress(OSError):
                os.unlink(path)
        for path, backup in backups.items():
            if os.path.exists(backup):
                os.replace(backup, path)
        raise
    finally:
        for _path, temp in staged:
            if os.path.exists(temp):
                os.unlink(temp)


def preserve(pages_dir):
    """Copy the currently verified generation into a physical LKG directory.

    The generation (feed pair, manifest, README pages, country pages) is built
    in a temporary directory and renamed into place before the pointer is
    replaced. A crash before the pointer leaves the old generation current; a
    crash after it leaves a complete new generation.
    """
    if not os.path.exists(os.path.join(pages_dir, MANIFEST_NAME)):
        return None
    manifest = verify(pages_dir)
    root = _root_of(pages_dir)
    lkg_root = _lkg_root(pages_dir)
    os.makedirs(lkg_root, exist_ok=True)
    generation = (
        "gen-" + time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        + f"-{time.time_ns() % 1_000_000_000:09d}"
    )
    target = os.path.join(lkg_root, generation)
    temp = os.path.join(lkg_root, f".tmp-{generation}")
    if os.path.exists(temp):
        shutil.rmtree(temp)
    os.makedirs(temp)
    try:
        for path in _surface_files(pages_dir):
            rel = _surface_rel(path, root)
            with open(path, "rb") as fh:
                data = fh.read()
            _write_bytes(os.path.join(temp, rel), data)
        os.replace(temp, target)
    finally:
        if os.path.exists(temp):
            shutil.rmtree(temp)

    pointer = {
        "generation": generation,
        "generated_at": manifest.get("generated_at"),
        "run_id": manifest.get("run_id"),
        "published": manifest.get("published"),
        "path": f"pages/{LKG_DIR}/{generation}",
    }
    _write_json(_current_path(pages_dir), pointer)

    generations = sorted(
        name for name in os.listdir(lkg_root) if name.startswith("gen-")
        and os.path.isdir(os.path.join(lkg_root, name))
    )
    for stale in generations[:-LKG_KEEP]:
        shutil.rmtree(os.path.join(lkg_root, stale), ignore_errors=True)
    return pointer


def recover(pages_dir):
    """Restore the newest complete LKG generation into the published paths."""
    pointer = read_last_known_good(pages_dir)
    generation = str((pointer or {}).get("generation") or "")
    if not generation:
        raise ManifestMismatch("no last-known-good generation is available")
    source = os.path.join(_lkg_root(pages_dir), generation)
    if not os.path.isdir(source):
        raise ManifestMismatch(f"last-known-good generation {generation} is missing")
    source_pages = _generation_pages(source)
    legacy = source_pages == source
    manifest = verify(source_pages)

    root = _root_of(pages_dir)
    pairs = []
    restored = set()
    for base, _dirs, names in os.walk(source):
        for name in names:
            full = os.path.join(base, name)
            rel = _surface_rel(full, source)
            if not _is_surface_rel(rel, legacy=legacy):
                continue
            dest_rel = _legacy_dest(rel) if legacy else rel
            with open(full, "rb") as fh:
                pairs.append((os.path.join(root, dest_rel), fh.read()))
            restored.add(dest_rel)

    if not pairs:
        raise ManifestMismatch(f"generation {generation} has no published files")
    _restore_pairs(pairs)

    # A page that exists only in the newer generation would still claim the
    # new run id; recovery removes it instead of leaving a mixed surface.
    surface = set()
    for pattern in SURFACE_PATTERNS:
        for path in glob.glob(os.path.join(root, pattern)):
            surface.add(_surface_rel(path, root))
    for rel in sorted(surface - restored):
        with contextlib.suppress(OSError):
            os.unlink(os.path.join(root, rel))

    verify(pages_dir)
    return manifest


def prepare(pages_dir):
    """Recover a torn generation, then preserve the verified current one.

    Called before a publication replaces the feed. If the current manifest does
    not match its files, a physical LKG is restored first; without one the
    publication is blocked rather than overwriting an unknown state.
    """
    manifest_path = os.path.join(pages_dir, MANIFEST_NAME)
    if not os.path.exists(manifest_path):
        return None
    try:
        verify(pages_dir)
    except ManifestMismatch:
        if has_last_known_good(pages_dir):
            return recover(pages_dir)
        raise
    return preserve(pages_dir)


def verify(pages_dir, manifest=None, check_rows=False):
    """Check manifest bytes and hashes against the files it describes.

    `check_rows` is opt-in because parsing the production feed doubles peak
    memory; normal runs rely on the writer, which built both the records and the
    manifest rows from the same list. Tests enable it.
    """
    manifest = manifest or read(os.path.join(pages_dir, MANIFEST_NAME))
    if not manifest:
        raise ManifestMismatch(f"{MANIFEST_NAME} is missing or unreadable")
    published = manifest.get("published")
    if not isinstance(published, dict):
        raise ManifestMismatch("manifest has no published section")

    for name in PUBLISHED_ARTIFACTS:
        entry = published.get(name)
        if not isinstance(entry, dict):
            raise ManifestMismatch(f"manifest has no published.{name}")
        path = os.path.join(pages_dir, f"{name}.json")
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            raise ManifestMismatch(f"{name}.json is unreadable: {exc}") from exc
        raw = text.encode("utf-8")
        if int(entry.get("bytes", -1)) != len(raw):
            raise ManifestMismatch(
                f"{name}.json bytes {len(raw)} != manifest {entry.get('bytes')}"
            )
        digest = hashlib.sha256(raw).hexdigest()
        if str(entry.get("sha256")) != digest:
            raise ManifestMismatch(f"{name}.json sha256 does not match manifest")
        if check_rows:
            try:
                rows = len(json.loads(text))
            except ValueError as exc:
                raise ManifestMismatch(f"{name}.json is not valid JSON: {exc}") from exc
            if int(entry.get("rows", -1)) != rows:
                raise ManifestMismatch(
                    f"{name}.json rows {rows} != manifest {entry.get('rows')}"
                )
    return manifest


def _published_counts(manifest):
    return {name: int(manifest["published"][name]["rows"]) for name in PUBLISHED_ARTIFACTS}


def build_reconciled(pages_dir, manifest):
    """The three legacy artifacts patched to describe `manifest`, not written.

    Returned as ``[(path, payload)]`` so the publication can include them in the
    same atomic write as the feed. Only existing files are patched; a missing
    legacy artifact is a finding for `audit`, not something to invent.
    """
    published = _published_counts(manifest)
    run_id = str(manifest.get("run_id") or "")
    pointer = f"pages/{MANIFEST_NAME}"
    out = []

    run_meta_path = os.path.join(pages_dir, "run_meta.json")
    run_meta = read(run_meta_path)
    if run_meta:
        run_meta["published"] = published
        run_meta["published_manifest"] = pointer
        # Existing consumers read `total`; keep it, but say which stage it is.
        run_meta["total_stage"] = "pre_write_candidate_rows"
        run_meta["generation"] = run_id
        run_meta[LEGACY_STATUS] = "published"
        out.append((run_meta_path, run_meta))

    for name in ("health.json", "source_stats.json"):
        path = os.path.join(pages_dir, name)
        payload = read(path)
        if not payload:
            continue
        section = payload.setdefault("run", {})
        section["published"] = published
        section["manifest"] = pointer
        section["generation"] = run_id
        section[LEGACY_STATUS] = "published"
        out.append((path, payload))

    return out


def reconcile(pages_dir, manifest=None, check_rows=False):
    """Verify the manifest, then make the legacy count artifacts agree.

    Returns the manifest. Raises `ManifestMismatch` before writing any legacy
    artifact when the feed and manifest disagree.
    """
    manifest = verify(pages_dir, manifest=manifest, check_rows=check_rows)
    _write_json_pairs(build_reconciled(pages_dir, manifest))
    return manifest


def mark_blocked(pages_dir, reason):
    """Point the legacy artifacts at the last published generation, not a run.

    A refused publication wrote `run_meta`/`health`/`source_stats` from candidate
    frames earlier in the run. Leaving those counts would advertise a generation
    that was never published, so they are repointed at the existing manifest and
    marked blocked. The feed and manifest are never touched here.
    """
    manifest = read(os.path.join(pages_dir, MANIFEST_NAME))
    published = None
    run_id = None
    if isinstance(manifest.get("published"), dict):
        try:
            published = _published_counts(manifest)
            run_id = str(manifest.get("run_id") or "")
        except (KeyError, TypeError, ValueError):
            published = None
            run_id = None
    pointer = f"pages/{MANIFEST_NAME}"
    payloads = []

    run_meta_path = os.path.join(pages_dir, "run_meta.json")
    run_meta = read(run_meta_path)
    if run_meta:
        if published is not None:
            run_meta["published"] = published
            run_meta["published_manifest"] = pointer
        run_meta["generation"] = run_id
        run_meta[LEGACY_STATUS] = "blocked"
        run_meta["publication_blocked_reason"] = str(reason)
        payloads.append((run_meta_path, run_meta))

    for name in ("health.json", "source_stats.json"):
        path = os.path.join(pages_dir, name)
        payload = read(path)
        if not payload:
            continue
        section = payload.setdefault("run", {})
        if published is not None:
            section["published"] = published
            section["manifest"] = pointer
        section["generation"] = run_id
        section[LEGACY_STATUS] = "blocked"
        section["publication_blocked_reason"] = str(reason)
        payloads.append((path, payload))

    if payloads:
        _write_json_pairs(payloads)
    return payloads


def audit(root_dir, check_rows=False):
    """Fail unless README, manifest, health, and feed describe one generation.

    `root_dir` is the repository root (the directory containing `pages/`), not
    the pages directory: the README and country pages live beside it.
    """
    pages_dir = os.path.join(root_dir, "pages")
    manifest = verify(pages_dir, check_rows=check_rows)
    run_id = str(manifest.get("run_id") or "")
    problems = []
    if not run_id:
        problems.append("manifest has no run_id")
    published = _published_counts(manifest) if run_id else {}

    checks = (
        ("run_meta.json", lambda p: (p.get("generation"), p.get("published"),
                                     p.get(LEGACY_STATUS))),
        ("health.json", lambda p: (p.get("run", {}).get("generation"),
                                   p.get("run", {}).get("published"),
                                   p.get("run", {}).get(LEGACY_STATUS))),
        ("source_stats.json", lambda p: (p.get("run", {}).get("generation"),
                                         p.get("run", {}).get("published"),
                                         p.get("run", {}).get(LEGACY_STATUS))),
        # The industry/verification report must describe the same generation as
        # the feed it sits beside, or the coverage numbers are unattributable.
        ("coverage.json", lambda p: (p.get("run", {}).get("generation"),
                                     p.get("run", {}).get("published"),
                                     p.get("run", {}).get(LEGACY_STATUS))),
    )
    for name, getter in checks:
        payload = read(os.path.join(pages_dir, name))
        if not payload:
            problems.append(f"{name} is missing")
            continue
        generation, counts, status = getter(payload)
        if generation != run_id:
            problems.append(f"{name} generation {generation!r} != {run_id!r}")
        if counts != published:
            problems.append(f"{name} published {counts!r} != {published!r}")
        if status != "published":
            problems.append(f"{name} status {status!r} is not 'published'")

    snap = read(os.path.join(pages_dir, "snapshots.json"))
    summary = (manifest.get("inputs") or {}).get("snapshots") or {}
    if not snap:
        problems.append("snapshots.json is missing")
    elif summary.get("generated_at") != snap.get("generated_at"):
        problems.append(
            "manifest snapshot ledger "
            f"{summary.get('generated_at')!r} != snapshots.json {snap.get('generated_at')!r}"
        )

    readme = (manifest.get("published") or {}).get("readme")
    if not isinstance(readme, dict):
        problems.append("manifest does not describe the README surface")
    elif readme.get("generation") != run_id:
        problems.append(
            f"README generation {readme.get('generation')!r} != {run_id!r}")
    else:
        page_paths = sorted([os.path.join(root_dir, "README.md")] + glob.glob(
            os.path.join(pages_dir, "README-*.md")))
        page_paths = [path for path in page_paths if os.path.exists(path)]
        if len(page_paths) != int(readme.get("pages", -1)):
            problems.append(
                f"README pages {len(page_paths)} != manifest {readme.get('pages')}")
        rows = 0
        for path in page_paths:
            try:
                with open(path, encoding="utf-8") as handle:
                    rows += handle.read().count("<tr>")
            except OSError as exc:
                problems.append(f"{os.path.relpath(path, root_dir)} unreadable: {exc}")
        # One <tr> is the table header on every page.
        if rows - len(page_paths) != int(readme.get("rows", -1)):
            problems.append(
                f"README rows {rows - len(page_paths)} != manifest {readme.get('rows')}"
            )

    expected = marker(run_id)
    for pattern in SURFACE_PATTERNS:
        for path in sorted(glob.glob(os.path.join(root_dir, pattern))):
            try:
                with open(path, encoding="utf-8") as handle:
                    text = handle.read()
            except OSError as exc:
                problems.append(f"{os.path.relpath(path, root_dir)} unreadable: {exc}")
                continue
            if expected not in text:
                problems.append(
                    f"{os.path.relpath(path, root_dir)} does not carry {run_id!r}"
                )

    if problems:
        raise ManifestMismatch("; ".join(problems))
    return manifest
