from app.services.youtube_service import classify_youtube_content

def test_gaming_category_rejected():
    assert classify_youtube_content({"snippet": {"categoryId": "20", "title": "Normal Title"}}) == "GAMING"

def test_music_category_rejected():
    assert classify_youtube_content({"snippet": {"categoryId": "10", "title": "Normal Title"}}) == "MUSIC"

def test_explicit_gameplay_rejected():
    assert classify_youtube_content({"snippet": {"categoryId": "22", "title": "Minecraft Gameplay 2026"}}) == "GAMING"

def test_official_music_video_rejected():
    assert classify_youtube_content({"snippet": {"categoryId": "22", "title": "Artist - Song (Official Video)"}}) == "MUSIC"

def test_official_audio_rejected():
    assert classify_youtube_content({"snippet": {"categoryId": "22", "title": "Artist - Song (Official Audio)"}}) == "MUSIC"

def test_lyrics_video_rejected():
    assert classify_youtube_content({"snippet": {"categoryId": "22", "title": "Artist - Song (Lyrics Video)"}}) == "MUSIC"

def test_gaming_livestream_rejected():
    assert classify_youtube_content({"snippet": {"categoryId": "22", "title": "24 Hour Twitch Stream Fortnite Gameplay"}}) == "GAMING"

def test_podcast_accepted():
    assert classify_youtube_content({"snippet": {"categoryId": "24", "title": "Podcast Episode 1"}}) == "PODCAST"

def test_interview_accepted():
    assert classify_youtube_content({"snippet": {"categoryId": "24", "title": "Interview with CEO"}}) == "INTERVIEW"

def test_vlog_accepted():
    assert classify_youtube_content({"snippet": {"categoryId": "22", "title": "Vlog: Day in the life"}}) == "VLOG"

def test_creator_commentary_accepted():
    assert classify_youtube_content({"snippet": {"categoryId": "24", "title": "Creator explains new rules"}}) == "COMMENTARY"

def test_storytime_accepted():
    assert classify_youtube_content({"snippet": {"categoryId": "22", "title": "Storytime: How it happened"}}) == "STORYTELLING"

def test_musician_interview_accepted():
    assert classify_youtube_content({"snippet": {"categoryId": "10", "title": "Interview with Taylor Swift"}}) == "INTERVIEW"

def test_gaming_industry_interview_accepted():
    assert classify_youtube_content({"snippet": {"categoryId": "20", "title": "Interview with game developer about new gameplay"}}) == "INTERVIEW"

def test_interview_with_game_developer():
    assert classify_youtube_content({"snippet": {"categoryId": "22", "title": "Interview with game developer"}}) == "INTERVIEW"

from app.services.youtube_service import calculate_creator_trend_score
from datetime import datetime, timezone, timedelta

def test_momentum_outranks_lifetime_popularity():
    now = datetime.now(timezone.utc)
    # Video A: 40M views, 2 years old
    pub_a = (now - timedelta(days=730)).strftime("%Y-%m-%dT%H:%M:%SZ")
    item_a = {
        "snippet": {"publishedAt": pub_a},
        "statistics": {"viewCount": "40000000"}
    }

    # Video B: 5M views, 18 hours old
    pub_b = (now - timedelta(hours=18)).strftime("%Y-%m-%dT%H:%M:%SZ")
    item_b = {
        "snippet": {"publishedAt": pub_b},
        "statistics": {"viewCount": "5000000"}
    }

    score_a = calculate_creator_trend_score(item_a)
    score_b = calculate_creator_trend_score(item_b)

    # B should have a much higher score because of massive momentum (5M/18h) vs A (40M/17520h)
    assert score_b > score_a

def test_seed_creator_bonus():
    now = datetime.now(timezone.utc)
    pub = (now - timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%SZ")

    item_normal = {
        "snippet": {"publishedAt": pub, "channelId": "random_channel"},
        "statistics": {"viewCount": "1000000"}
    }

    item_seed = {"seed_creator": True,
        "snippet": {"publishedAt": pub, "channelId": "UCX6OQ3DkcsbYNE6H8uQQuVA"}, # MrBeast
        "statistics": {"viewCount": "1000000"}
    }

    assert calculate_creator_trend_score(item_seed) > calculate_creator_trend_score(item_normal)
