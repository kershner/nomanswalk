"""Discover, audit, and rotate Bluesky tags for No Man's Walk clips."""

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
import math
import os
import random
import re
from statistics import median

from atproto_client.models.app.bsky.feed.search_posts import Params as SearchParams


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TAG_POOL_FILE = os.path.join(BASE_DIR, "bluesky_tag_pool.json")
CORE_TAGS = ("nomanssky", "nms", "hellogames")
SEED_TAGS = CORE_TAGS + (
    "virtualphotography",
    "gaming",
    "twitchclips",
    "spacegame",
)
FALLBACK_TAGS = (
    "spacegame",
    "scifi",
    "exploration",
    "virtualphotography",
    "gamephotography",
    "gamescreenshots",
    "alienworlds",
    "exoplanets",
    "space",
    "proceduralgeneration",
    "openworld",
    "cozygaming",
    "cozygames",
    "chillgaming",
    "gamingcommunity",
    "blueskygaming",
    "twitchstreamer",
    "twitchclips",
    "gameclips",
    "pcgaming",
    "gaming",
    "videogames",
    "streaming",
    "gamedev",
)
BLOCKED_TAGS = {
    "nsfw",
    "politics",
    "giveaway",
    "giveaways",
    "followback",
    "webdev",
    "javascript",
    "django",
    "obs",
    "gameautomation",
    "gamingbot",
}
TAG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
MAX_DISCOVERED_CANDIDATES = 25
MAX_POOL_SIZE = 20
MIN_POOL_SIZE = 10
REFRESH_AFTER = timedelta(days=7)


def _search_posts(client, tag, *, since, until=None, limit=100):
    """Return raw posts so newer embed types do not break an older SDK."""
    params = SearchParams(
        q=f"#{tag}",
        tag=[tag],
        limit=limit,
        sort="latest",
        since=since,
        until=until,
    )
    response = client.app.bsky.feed._client.invoke_query(
        "app.bsky.feed.searchPosts",
        params=params,
        output_encoding="application/json",
    )
    return list(response.content.get("posts", []))


def _post_tags(post):
    tags = set()
    for facet in post.get("record", {}).get("facets", []):
        for feature in facet.get("features", []):
            if feature.get("$type") == "app.bsky.richtext.facet#tag":
                tag = str(feature.get("tag", "")).casefold()
                if TAG_PATTERN.fullmatch(tag):
                    tags.add(tag)
    return tags


def _created_at(post):
    value = post.get("record", {}).get("createdAt")
    if not value:
        return None
    value = re.sub(
        r"(\.\d{6})\d+(?=(?:Z|[+-]\d{2}:\d{2})$)",
        r"\1",
        str(value),
    ).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _engagement(post):
    return sum(
        post.get(field) or 0
        for field in ("likeCount", "repostCount", "replyCount", "quoteCount")
    )


def _discover_candidates(client, own_did, now):
    since = (now - timedelta(days=30)).isoformat().replace("+00:00", "Z")
    post_counts = Counter()
    authors = defaultdict(set)
    core_tags = set(CORE_TAGS)

    for seed in SEED_TAGS:
        for post in _search_posts(client, seed, since=since):
            author_did = post.get("author", {}).get("did")
            if not author_did or author_did == own_did:
                continue
            for tag in _post_tags(post) - core_tags:
                if tag in BLOCKED_TAGS:
                    continue
                post_counts[tag] += 1
                authors[tag].add(author_did)

    eligible = [
        tag
        for tag, count in post_counts.items()
        if count >= 2 and len(authors[tag]) >= 2
    ]
    eligible.sort(
        key=lambda tag: (post_counts[tag], len(authors[tag])),
        reverse=True,
    )
    return Counter(
        {tag: post_counts[tag] for tag in eligible[:MAX_DISCOVERED_CANDIDATES]}
    )


def _audit_candidate(client, tag, own_did, now):
    posts = _search_posts(
        client,
        tag,
        since=(now - timedelta(days=30)).isoformat().replace("+00:00", "Z"),
        until=(now - timedelta(hours=24)).isoformat().replace("+00:00", "Z"),
    )
    posts = [
        post
        for post in posts
        if post.get("author", {}).get("did") not in (None, own_did)
    ]
    recent_posts = [
        post
        for post in posts
        if (created_at := _created_at(post))
        and created_at >= now - timedelta(days=7)
    ]
    author_counts = Counter(post.get("author", {}).get("did") for post in posts)
    interactions = [_engagement(post) for post in posts]
    ordered = sorted(interactions)
    p75 = ordered[round((len(ordered) - 1) * 0.75)] if ordered else 0
    return {
        "posts_7d": len(recent_posts),
        "posts_30d": len(posts),
        "authors_30d": len(author_counts),
        "top_author_share": max(author_counts.values()) / len(posts) if posts else 1.0,
        "median_engagement": median(interactions) if interactions else 0,
        "p75_engagement": p75,
    }


def _candidate_score(metrics, cooccurrences):
    engagement = 1 + metrics["median_engagement"] + 0.25 * metrics["p75_engagement"]
    activity = math.log2(1 + min(metrics["posts_7d"], 100))
    diversity = math.log2(1 + metrics["authors_30d"])
    relevance = 1 + 0.1 * math.log2(1 + cooccurrences)
    return round(engagement * activity * diversity * relevance, 3)


def _load_pool(pool_file=TAG_POOL_FILE):
    try:
        with open(pool_file, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def pool_is_stale(now=None, pool_file=TAG_POOL_FILE):
    now = now or datetime.now(timezone.utc)
    updated_at = _load_pool(pool_file).get("updated_at")
    if not updated_at:
        return True
    try:
        updated = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
    except ValueError:
        return True
    return now - updated >= REFRESH_AFTER


def refresh_tag_pool(client, own_did, now=None, pool_file=TAG_POOL_FILE):
    """Discover and save a new pool, leaving the old file alone on failure."""
    now = now or datetime.now(timezone.utc)
    discovered = _discover_candidates(client, own_did, now)
    ranked = []

    for tag in set(FALLBACK_TAGS) | set(discovered):
        if tag in CORE_TAGS or tag in BLOCKED_TAGS or not TAG_PATTERN.fullmatch(tag):
            continue
        metrics = _audit_candidate(client, tag, own_did, now)
        if (
            metrics["posts_7d"] < 2
            or metrics["authors_30d"] < 5
            or metrics["median_engagement"] < 1
            or metrics["top_author_share"] > 0.35
        ):
            continue
        ranked.append(
            {
                "tag": tag,
                "score": _candidate_score(metrics, discovered[tag]),
                **metrics,
            }
        )

    ranked.sort(key=lambda item: (item["score"], item["tag"]), reverse=True)
    ranked = ranked[:MAX_POOL_SIZE]
    if len(ranked) < MIN_POOL_SIZE:
        raise RuntimeError(
            f"Bluesky tag audit produced only {len(ranked)} qualifying tags"
        )

    payload = {
        "updated_at": now.isoformat().replace("+00:00", "Z"),
        "tags": ranked,
    }
    temp_file = f"{pool_file}.tmp"
    with open(temp_file, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    os.replace(temp_file, pool_file)
    return payload


def active_rotating_tags(pool_file=TAG_POOL_FILE):
    tags = list(
        dict.fromkeys(
            item.get("tag")
            for item in _load_pool(pool_file).get("tags", [])
            if isinstance(item, dict) and item.get("tag")
        )
    )
    return tags if len(tags) >= 3 else list(FALLBACK_TAGS)


def select_video_tags():
    pool = [tag for tag in active_rotating_tags() if tag not in CORE_TAGS]
    return list(CORE_TAGS) + random.sample(pool, 3)
