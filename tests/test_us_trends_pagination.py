from collections import Counter
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock
import pytest
from app.services.youtube_service import YouTubeService
from app.services.creator_metadata import duration_metadata, language_metadata, classify_youtube_content
from app.config import config


def video(i, **snippet):
    return {'id': f'v{i:010d}', 'snippet': {'categoryId':'22', 'channelId':f'c{i % 25}',
        'title':f'My creator challenge with friends {i}', 'defaultAudioLanguage':'en-US',
        'publishedAt':(datetime.now(timezone.utc)-timedelta(hours=12)).isoformat(), **snippet},
        'contentDetails':{'duration':'PT1H4M38S' if i % 2 else 'PT8M42S'},
        'statistics':{'viewCount':'10000'}, 'status':{'privacyStatus':'public', 'uploadStatus':'processed'}}


def service_with_pages(tmp_path, monkeypatch, pages):
    monkeypatch.setattr(config, 'CREATOR_SEED_HANDLES', ())
    service=YouTubeService(tmp_path)
    service.api_key='test'
    client=MagicMock()
    for resource in ('search','channels','playlistItems'):
        getattr(client,resource).return_value.list.return_value.execute.return_value={'items':[]}
    def listing(**kw):
        assert kw['regionCode']=='US'
        result=MagicMock()
        result.execute.return_value=pages[int(kw.get('pageToken',0))]
        return result
    client.videos.return_value.list.side_effect=listing
    service._get_client=lambda:client
    return service,client


def test_trends_pagination_language_duration_and_no_loss(tmp_path, monkeypatch):
    pages=[{'items':[video(i) for i in range(35)]+[video(900+i,defaultAudioLanguage='tr') for i in range(15)], 'nextPageToken':'1'},
           {'items':[video(i) for i in range(35,80)]}]
    service,client=service_with_pages(tmp_path,monkeypatch,pages)
    first,token=service.get_us_trends()
    assert len(first)==50 and token
    second,end=service.get_us_trends(page_token=token)
    assert len(second)==30 and end is None
    assert not {v['id'] for v in first}&{v['id'] for v in second}
    assert max(Counter(v['channel_id'] for v in first).values())<=3
    assert service.last_diagnostics['non_english_rejected']==15
    assert service.last_diagnostics['eligible']==80
    assert all(v['language_status']=='ENGLISH' for v in first+second)
    assert {v['duration_display'] for v in first+second}=={'8:42','1:04:38'}
    before=client.videos.return_value.list.call_count
    assert service.get_us_trends()[0]==first
    assert service.get_us_trends(page_token=token)[0]==second
    assert client.videos.return_value.list.call_count==before==2
    with pytest.raises(ValueError): service.get_us_trends(page_token='../../bad')


def test_missing_duration_and_long_video_are_not_quality_gates(tmp_path, monkeypatch):
    items=[video(1),video(2),video(3)]
    items[0]['contentDetails']['duration']='PT2H17M5S'
    items[1]['contentDetails']={}
    items[2]['contentDetails']['duration']='garbage'
    service,_=service_with_pages(tmp_path,monkeypatch,[{'items':items}])
    result,_=service.get_us_trends()
    assert len(result)==3
    assert next(v for v in result if v['id']==items[0]['id'])['duration_display']=='2:17:05'
    assert sum(v['duration_display']=='Unavailable' for v in result)==2


@pytest.mark.parametrize('raw,seconds,label',[
    ('PT8M42S',522,'8:42'),('PT27M16S',1636,'27:16'),('PT59S',59,'0:59'),
    ('PT1H',3600,'1:00:00'),('PT1H4M38S',3878,'1:04:38'),('PT2H17M5S',8225,'2:17:05'),
    (None,None,'Unavailable'),('',None,'Unavailable'),('PT0S',None,'Unavailable'),('PT1Hx',None,'Unavailable')])
def test_duration(raw,seconds,label):
    assert duration_metadata(raw)==(seconds,label)


@pytest.mark.parametrize('language,result',[('en','ENGLISH'),('en-US','ENGLISH'),('en-GB','ENGLISH'),('tr','NON_ENGLISH'),('es','NON_ENGLISH')])
def test_explicit_language(language,result):
    assert language_metadata(video(1,defaultAudioLanguage=language))[0]==result


def test_language_unknown_is_conservative():
    assert language_metadata({'snippet':{'title':'gato perro libro'}})[0]=='UNKNOWN'
    assert language_metadata({'snippet':{'title':'How I built my business'}})[0]=='ENGLISH'
    assert language_metadata({'snippet':{'title':'How I built my business','defaultAudioLanguage':'es'}})[0]=='NON_ENGLISH'


@pytest.mark.parametrize('title,category,kind',[
    ('IShowSpeed IRL travel','20','IRL'),('Kai Cenat Fortnite gameplay challenge','22','GAMING'),
    ('My challenge with friends','22','CHALLENGE'),('Our new collaboration','22','COLLABORATION'),
    ('Reaction to the movie trailer','24','REACTION'),('Fortnite esports final','22','GAMING'),
    ('New movie official trailer','24','TRAILER'),('TV series teaser','24','TRAILER'),
    ('Studio promo for the movie','24','TRAILER'),('Movie scene compilation','24','MOVIE_TV_CLIP'),
    ('Full match football','17','SPORTS'),('Rain sounds for sleep','22','AMBIENT'),
    ('Interview with a musician','10','INTERVIEW'),('Podcast on game design','20','PODCAST'),
    ('My vlog in Japan','22','VLOG'),('Artist official audio','22','MUSIC')])
def test_video_formats(title,category,kind):
    assert classify_youtube_content(video(1,title=title,categoryId=category))==kind


def test_verified_seed_and_dynamic_search_share_video_gates(tmp_path, monkeypatch):
    monkeypatch.setattr(config,'CREATOR_SEED_HANDLES',('@Seed',))
    service=YouTubeService(tmp_path)
    seed_game=video(1,channelId='verified-seed',title='Fortnite Gameplay',categoryId='20')
    organic=video(2,channelId='organic',title='My interview with a creator')
    seed_irl=video(3,channelId='verified-seed',title='My IRL travel vlog',categoryId='20')
    calls=[]
    class API:
        def __getattr__(self,resource):
            def endpoint():
                def listing(**params):
                    calls.append((resource,params))
                    result={'items':[]}
                    if resource=='videos':
                        result={'items':[seed_game] if 'chart' in params else [v for v in [organic,seed_irl] if v['id'] in params['id'].split(',')]}
                    elif resource=='search':result={'items':[{'id':{'videoId':organic['id']}}]}
                    elif resource=='channels':
                        ids=['verified-seed'] if 'forHandle' in params else params['id'].split(',')
                        result={'items':[{'id':cid,'snippet':{'title':cid,'country':'US'},'contentDetails':{'relatedPlaylists':{'uploads':'uploads-'+cid}}} for cid in ids]}
                    elif resource=='playlistItems':result={'items':[{'contentDetails':{'videoId':seed_irl['id']}}]}
                    reply=MagicMock();reply.execute.return_value=result;return reply
                endpoint=MagicMock();endpoint.list=listing;return endpoint
            return endpoint
    service._get_client=lambda:API()
    results,_=service.get_us_trends()
    assert {v['id'] for v in results}=={organic['id'],seed_irl['id']}
    assert service.last_diagnostics['verified_seeds']['verified-seed']['handle']=='@Seed'
    assert service.last_diagnostics['gaming_rejected']==1
    assert any(p.get('playlistId')=='uploads-organic' for r,p in calls)
    assert all(p['regionCode']=='US' for r,p in calls if r=='search' or p.get('chart'))
    assert all(len(p['id'].split(','))<=50 for r,p in calls if 'id' in p)
    assert len(calls)<=40


def test_growth_score_uses_observed_snapshot(tmp_path,monkeypatch):
    import json,time
    from app.services.creator_trends import CreatorTrends
    item=video(1)
    service,_=service_with_pages(tmp_path,monkeypatch,[{'items':[item]}])
    engine=CreatorTrends(service)
    (engine.root/'view-snapshots.json').write_text(json.dumps({item['id']:{'timestamp':time.time()-3600,'views':6400}}))
    result,_=engine.get()
    assert result[0]['momentum_source']=='OBSERVED_GROWTH'
    assert 3590<result[0]['views_per_hour']<3610


def test_api_discovery_call_budget(tmp_path):
    from app.services.creator_trends import CreatorTrends
    service=YouTubeService(tmp_path)
    client=MagicMock();client.videos.return_value.list.return_value.execute.return_value={'items':[]}
    service._get_client=lambda:client
    engine=CreatorTrends(service)
    for i in range(55):engine.api('videos',id=str(i))
    assert engine.calls==40
    assert client.videos.return_value.list.call_count==40

@pytest.mark.parametrize('snippet',[
    {'title':'Thursday night stream','description':'00:30 R6 Ranked\n2:00 Minecraft Hardcore Challenge'},
    {'title':'We get jump scared in Minecraft lucky blocks'},
    {'title':'I pushed the needle in a haystack game to its limits','description':'Game: https://store.steampowered.com/app/123'},
    {'title':'One of the hardest fights! Stage 7','tags':['DBZ Dokkan Battle','rare summons']},
])
def test_live_metadata_gaming_regressions(snippet):
    assert classify_youtube_content(video(1,categoryId='24',**snippet))=='GAMING'
