"""Cheap, explainable video-level metadata filters. No downloads or AI calls."""
import math
import re
from datetime import datetime, timezone


def duration_metadata(value):
    match = re.fullmatch(r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value or "")
    if not match or not any(match.groups()):
        return None, "Unavailable"
    days, hours, minutes, seconds = (int(n or 0) for n in match.groups())
    total = days * 86400 + hours * 3600 + minutes * 60 + seconds
    if total <= 0:
        return None, "Unavailable"
    return total, (f"{total // 3600}:{total // 60 % 60:02d}:{total % 60:02d}" if total >= 3600
                   else f"{total // 60}:{total % 60:02d}")


def language_metadata(item):
    snippet = item.get("snippet", {})
    for key in ("defaultAudioLanguage", "defaultLanguage"):
        if snippet.get(key):
            return ("ENGLISH" if snippet[key].lower().split("-")[0] == "en"
                    else "NON_ENGLISH"), key
    title = snippet.get("title", "").lower()
    text = title + " " + snippet.get("description", "")[:1200].lower()
    words = set(re.findall(r"\b[a-z]+\b", text))
    title_words = set(re.findall(r"\b[a-z]+\b", title))
    english = {"the", "and", "with", "this", "that", "how", "why", "your", "you", "my", "i", "we", "our", "what", "from", "for", "into", "about", "is", "to"}
    other = {"que", "los", "las", "una", "para", "con", "por", "pero", "como", "não", "bir", "ile", "için", "ve", "çok", "les", "une", "avec", "pour"}
    if len(words & other) >= 3 or (text and sum(ord(c) < 128 for c in text) / len(text) < .7):
        return "NON_ENGLISH", "metadata_heuristic"
    if len(title_words & english) >= 2 or (len(title_words & english) >= 1 and len(words & english) >= 4):
        return "ENGLISH", "metadata_heuristic"
    return "UNKNOWN", "insufficient_metadata"


def classify_youtube_content(item):
    s = item.get("snippet", {})
    title = s.get("title", "").lower().replace("’", "'")
    description = s.get("description", "").lower()[:3000]
    tags = " ".join(s.get("tags") or []).lower()
    category = str(s.get("categoryId", ""))
    # These explicitly describe a spoken format, even when its subject is gaming/music.
    spoken = [(r"\binterviews?\b", "INTERVIEW"), (r"\bpodcast\b", "PODCAST"),
              (r"\b(reacts? to|reacting to|reaction to)\b", "REACTION")]
    for pattern, kind in spoken:
        if re.search(pattern, title):
            return kind
    if re.search(r"\b(official (music )?video|official audio|music video|lyric[s]? video|lyrics|full album|visualizer|dj mix|live performance|full concert)\b", title):
        return "MUSIC"
    if re.search(r"\b(trailer|teaser|studio promo|sneak peek)\b", title):
        return "TRAILER"
    if re.search(r"\b(movie clip|tv clip|full episode|movie scene|film scene)\b", title):
        return "MOVIE_TV_CLIP"
    if re.search(r"\b(gameplay|walkthrough|speedrun|let's play|playthrough|esports|ranked match|cash cup|fncs)\b", title):
        return "GAMING"
    if re.search(r"\b(full match|match highlights|game highlights|live play by play)\b", title):
        return "SPORTS"
    if re.search(r"\b(ambient|background music|sleep music|rain sounds|white noise)\b", title):
        return "AMBIENT"
    irl = re.search(r"\b(irl|vlog|travel vlog|day in the life|morning routine)\b", title)
    if category == "20" and not irl:
        return "GAMING"
    if category == "10":
        return "MUSIC"
    game = r"\b(minecraft|fortnite|roblox|valorant|gta\s*[56v]?|overwatch|brawl stars|dokkan battle|rainbow six|r6 ranked)\b"
    # Format evidence from the video itself, never a creator blacklist. Vlogs
    # and explicit spoken formats can cover games without being gameplay.
    if not irl and (re.search(game, title) or
                   re.search(r"\bthe game\b", title) or
                   re.search(r"#(?:gaming|gameplay|minecraft|dokkanbattle)\b", description) or
                   ("stream" in title and re.search(game, description)) or
                   (re.search(r"\b(game|playing)\b", title) and "store.steampowered.com/app/" in description) or
                   (re.search(game, tags) and re.search(r"\b(stage\s*\d|fights?|summons?|pulls|ranked)\b", title))):
        return "GAMING"
    formats = [(r"\birl\b", "IRL"), (r"\b(vlog|day in the life|morning routine)\b", "VLOG"),
               (r"\b(challenge|i spent|i tried|survive|hide and seek)\b", "CHALLENGE"),
               (r"\b(reacts|reaction)\b", "REACTION"), (r"\b(collab|collaboration|featuring)\b", "COLLABORATION"),
               (r"\b(social experiment)\b", "SOCIAL_EXPERIMENT"),
               (r"\b(behind the scenes)\b", "BEHIND_THE_SCENES"),
               (r"\b(documentary|the truth about|how i built)\b", "CREATOR_DOCUMENTARY"),
               (r"\b(storytime|my story|how it happened)\b", "STORYTELLING"),
               (r"\b(talks to|we need to talk|discussion|q&a|conversation)\b", "CONVERSATION"),
               (r"\b(explains|review|commentary)\b", "COMMENTARY")]
    for pattern, kind in formats:
        if re.search(pattern, title):
            return kind
    if category == "17":
        return "SPORTS"
    return "OTHER_CREATOR_CONTENT" if category in {"22", "23", "24", "26", "27", "28"} else "UNKNOWN"


def age_hours(item):
    try:
        published = datetime.fromisoformat(item.get("snippet", {}).get("publishedAt", "").replace("Z", "+00:00"))
        return max(1, (datetime.now(timezone.utc) - published).total_seconds() / 3600)
    except (ValueError, TypeError):
        return 24 * 365


def calculate_creator_trend_score(item):
    age = age_hours(item)
    views = int(item.get("statistics", {}).get("viewCount") or 0)
    observed = item.get("observed_views_per_hour")
    rate = observed if observed is not None else views / age
    momentum = min(1, math.log1p(max(0, rate)) / math.log1p(500000))
    recency = 1 / (1 + age / 72)
    # A small seed boost, never a whitelist. All channels use the same gates.
    seed = item.get("seed_creator", False)
    score = .65 * momentum + .30 * recency + (.05 if seed else 0)
    if age > 30 * 24 and observed is None:
        score *= .05
    return round(score, 5)
