"""Bounded official API discovery with stable, cached, diverse feed pages."""
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import time
import uuid

from app.config import config
from app.services.creator_metadata import (age_hours, classify_youtube_content,
    calculate_creator_trend_score, duration_metadata, language_metadata)
from app.services.json_storage import atomic_write_json
from app.services.source_rights_service import SourceRightsService

VERSION = 3
REJECTED = {"GAMING": "gaming_rejected", "MUSIC": "music_rejected",
            "TRAILER": "trailer_rejected", "MOVIE_TV_CLIP": "trailer_rejected"}


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


class CreatorTrends:
    def __init__(self, service):
        self.service = service
        self.root = service.cache_dir / "creator-v1"
        self.root.mkdir(parents=True, exist_ok=True)
        self.calls = 0
        self.errors = []
        self.blocked = False
        self.client = None

    def api(self, resource, ttl=900, **params):
        key = hashlib.sha256(json.dumps([resource, params], sort_keys=True).encode()).hexdigest()
        path = self.root / f"api-{key}.json"
        saved = read_json(path)
        if time.time() - saved.get("timestamp", 0) < ttl:
            return saved["response"]
        if self.calls >= 40 or self.blocked:
            return {}
        self.calls += 1
        try:
            self.client = self.client or self.service._get_client()
            result = getattr(self.client, resource)().list(**params).execute()
            if not isinstance(result, dict):
                return {}
            atomic_write_json(path, {"timestamp": time.time(), "response": result})
            return result
        except Exception as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            self.errors.append({"resource": resource, "http_status": status,
                                "code": "YOUTUBE_API_UNAVAILABLE"})
            if status in (401, 403, 429):
                self.blocked = True
            return {}

    def get(self, limit=50, force=False, token=None):
        limit = max(1, min(50, int(limit)))
        if token:
            match = re.fullmatch(r"([a-f0-9]{32}):(\d{1,6})", token)
            if not match:
                raise ValueError("INVALID_TRENDS_PAGE")
            snapshot_id, offset = match[1], int(match[2])
            snapshot = read_json(self.root / f"feed-{snapshot_id}.json")
            if not snapshot or time.time() - snapshot["timestamp"] > 6 * 3600:
                raise ValueError("TRENDS_PAGE_EXPIRED")
        else:
            latest = read_json(self.root / "latest.json")
            snapshot_id = latest.get("id", "")
            snapshot = read_json(self.root / f"feed-{snapshot_id}.json") if re.fullmatch(r"[a-f0-9]{32}", snapshot_id) else {}
            ttl = 60 if force else 900
            if (not snapshot or snapshot.get("version") != VERSION or
                    snapshot.get("rights_cache_version") != self.service.RIGHTS_CACHE_VERSION or
                    time.time() - snapshot["timestamp"] >= ttl):
                snapshot = self.discover()
                snapshot_id = uuid.uuid4().hex
                atomic_write_json(self.root / f"feed-{snapshot_id}.json", snapshot)
                atomic_write_json(self.root / "latest.json", {"id": snapshot_id})
            offset = 0
        videos = snapshot["videos"]
        self.service.last_diagnostics = snapshot["diagnostics"]
        boundary = next((end for end in snapshot.get("page_ends", []) if end > offset), len(videos))
        end = min(offset + limit, boundary)
        page = videos[offset:end]
        next_token = f"{snapshot_id}:{end}" if end < len(videos) else None
        return page, next_token

    def discover(self):
        raw = {}
        origins = {}
        def add(items, origin):
            for item in items:
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    raw[item["id"]] = item
                    origins.setdefault(item["id"], set()).add(origin)
        page, visited = None, set()
        for _ in range(5):
            params = dict(part="snippet,statistics,contentDetails,status", chart="mostPopular",
                          regionCode="US", maxResults=50)
            if page:
                params["pageToken"] = page
            result = self.api("videos", **params)
            add(result.get("items", []), "US_MOST_POPULAR")
            page = result.get("nextPageToken")
            if not page or page in visited:
                break
            visited.add(page)
        # Queries discover creators dynamically, independently of the seed list.
        extra_ids = set()
        after = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT00:00:00Z")
        for query in ("I spent challenge", "travel IRL vlog", "creator interview podcast", "storytime commentary"):
            result = self.api("search", part="snippet", type="video", q=query,
                              regionCode="US", relevanceLanguage="en", order="viewCount",
                              publishedAfter=after, maxResults=50)
            for item in result.get("items", []):
                video_id = item.get("id", {}).get("videoId")
                if video_id:
                    extra_ids.add(video_id)
                    origins.setdefault(video_id, set()).add("RECENT_US_SEARCH")
        self.details(extra_ids - raw.keys(), add, "RECENT_US_SEARCH")

        channels, seeds = {}, {}
        for handle in config.CREATOR_SEED_HANDLES:
            result = self.api("channels", ttl=86400, part="snippet,contentDetails,statistics", forHandle=handle)
            for channel in result.get("items", []):
                cid = channel.get("id")
                if cid:
                    channels[cid] = channel
                    seeds[cid] = {"handle": handle, "title": channel.get("snippet", {}).get("title", "")}
        dynamic = Counter(item.get("snippet", {}).get("channelId") for item in raw.values()
                          if language_metadata(item)[0] == "ENGLISH" and
                          classify_youtube_content(item) not in {*REJECTED, "SPORTS", "AMBIENT", "UNKNOWN"})
        # Six organic channels plus the seeds. Cap coverage calls, not eligibility.
        dynamic_ids = [cid for cid, _ in dynamic.most_common() if cid and cid not in seeds][:6]
        all_ids = {item.get("snippet", {}).get("channelId") for item in raw.values()} - {None, ""}
        for batch in chunks(sorted(all_ids - channels.keys()), 50):
            result = self.api("channels", ttl=86400, part="snippet,contentDetails,statistics", id=",".join(batch), maxResults=50)
            channels.update({c["id"]: c for c in result.get("items", []) if c.get("id")})
        uploads = set()
        for cid in list(seeds) + dynamic_ids:
            playlist = channels.get(cid, {}).get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
            if not playlist:
                continue
            result = self.api("playlistItems", part="contentDetails", playlistId=playlist, maxResults=20)
            for item in result.get("items", []):
                vid = item.get("contentDetails", {}).get("videoId")
                if vid:
                    uploads.add(vid)
                    origins.setdefault(vid, set()).add("SEED_UPLOADS" if cid in seeds else "DISCOVERED_CREATOR_UPLOADS")
        self.details(uploads - raw.keys(), add, "CREATOR_UPLOADS")

        snapshots = read_json(self.root / "view-snapshots.json")
        counts = Counter(raw=len(raw), eligible=0, non_english_rejected=0,
                         gaming_rejected=0, music_rejected=0, trailer_rejected=0, other_rejected=0)
        eligible, audit = [], []
        now = time.time()
        for vid, item in raw.items():
            snippet = item.get("snippet", {})
            language, basis = language_metadata(item)
            kind = classify_youtube_content(item)
            seconds, display = duration_metadata(item.get("contentDetails", {}).get("duration"))
            status = item.get("status", {})
            reason = ("non_english_rejected" if language != "ENGLISH" else
                      REJECTED.get(kind) or ("other_rejected" if kind in {"SPORTS", "AMBIENT", "UNKNOWN"} else None))
            if snippet.get("liveBroadcastContent") in {"live", "upcoming"} or status.get("privacyStatus", "public") != "public":
                reason = reason or "other_rejected"
            audit.append({"id": vid, "title": snippet.get("title"), "channel_id": snippet.get("channelId"),
                          "language_status": language, "language_basis": basis, "content_type": kind,
                          "category_id": snippet.get("categoryId"), "duration_iso": item.get("contentDetails", {}).get("duration"),
                          "reason": reason, "discovery_sources": sorted(origins.get(vid, []))})
            if reason:
                counts[reason] += 1
                continue
            views = int(item.get("statistics", {}).get("viewCount") or 0)
            prior = snapshots.get(vid)
            observed = None
            if prior and now - prior["timestamp"] >= 900 and views >= prior["views"]:
                observed = (views - prior["views"]) * 3600 / (now - prior["timestamp"])
            if not prior or now - prior["timestamp"] >= 900:
                snapshots[vid] = {"timestamp": now, "views": views}
            item.update(observed_views_per_hour=observed, seed_creator=snippet.get("channelId") in seeds)
            rights = SourceRightsService.assess_rights(item, owned_channel_id=config.YOUTUBE_OWNED_CHANNEL_ID)
            channel = channels.get(snippet.get("channelId"), {})
            eligible.append({"id": vid, "title": snippet.get("title", ""), "channel": snippet.get("channelTitle", ""),
                "channel_id": snippet.get("channelId", ""), "channel_country": channel.get("snippet", {}).get("country"),
                "subscriber_count": channel.get("statistics", {}).get("subscriberCount"),
                "thumbnail": snippet.get("thumbnails", {}).get("high", {}).get("url", ""),
                "viewCount": views, "views": f"{views:,}", "published_at": snippet.get("publishedAt", ""),
                "duration_seconds": seconds, "duration_display": display,
                "duration_iso": item.get("contentDetails", {}).get("duration"),
                "language_status": language, "language_basis": basis, "content_type": kind,
                "creator_trend_score": calculate_creator_trend_score(item),
                "views_per_hour": round(observed if observed is not None else views / age_hours(item), 1),
                "momentum_source": "OBSERVED_GROWTH" if observed is not None else "UPLOAD_AVERAGE",
                "discovery_sources": sorted(origins.get(vid, [])), "seed_creator": item["seed_creator"],
                "rights_status": rights["rights_status"], "rights_reason": rights["rights_reason"],
                "rights_evidence": rights["rights_reason"], "privacy_status": status.get("privacyStatus", ""),
                "upload_status": status.get("uploadStatus", "")})
        atomic_write_json(self.root / "view-snapshots.json", {k: v for k, v in snapshots.items() if now - v["timestamp"] < 30 * 86400})
        eligible.sort(key=lambda v: v["creator_trend_score"], reverse=True)
        # Keep the established 3/channel visible-batch policy; defer excess rather than lose it.
        ordered, page_ends = [], []
        while eligible:
            per_channel, batch, remaining = Counter(), [], []
            for item in eligible:
                if len(batch) < 50 and per_channel[item["channel_id"]] < 3:
                    batch.append(item)
                    per_channel[item["channel_id"]] += 1
                else:
                    remaining.append(item)
            ordered.extend(batch)
            page_ends.append(len(ordered))
            eligible = remaining
        counts.update(eligible=len(ordered), unique_creators=len({v["channel_id"] for v in ordered}))
        diagnostics = {**counts, "region": "US", "api_calls": self.calls, "api_budget": 40,
                       "errors": self.errors, "verified_seeds": seeds,
                       "classification_audit": audit, "provider_next_page_token": page}
        if not raw and self.errors:
            raise RuntimeError("YOUTUBE_API_UNAVAILABLE")
        return {"version": VERSION, "rights_cache_version": self.service.RIGHTS_CACHE_VERSION,
                "timestamp": now, "videos": ordered,
                "page_ends": page_ends, "diagnostics": diagnostics}

    def details(self, ids, add, origin):
        for batch in chunks(sorted(ids), 50):
            response = self.api("videos", part="snippet,statistics,contentDetails,status", id=",".join(batch), maxResults=50)
            add(response.get("items", []), origin)


def chunks(items, size):
    for offset in range(0, len(items), size):
        yield items[offset:offset + size]
