from concurrent.futures import ThreadPoolExecutor

import pytest

from app.services.job_store import JobStore


def test_failed_serialization_does_not_truncate_existing_record(tmp_path):
    store = JobStore()
    store.jobs_dir = tmp_path
    store.set('job', {'status': 'COMPLETED'})
    with pytest.raises(ValueError):
        store.set('job', {'bad': float('nan')})
    assert store.get('job') == {'status': 'COMPLETED'}
    assert not list(tmp_path.glob('*.tmp'))


def test_concurrent_updates_and_reads_keep_complete_records(tmp_path):
    store = JobStore()
    store.jobs_dir = tmp_path
    store.set('job', {'count': 0, 'preserved': 'youtube-id'})
    def increment(_):
        for i in range(12):
            store.update('job', lambda job: job.update(count=job['count'] + 1))
            assert store.get('job')['preserved'] == 'youtube-id'
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(increment, range(4)))
    assert store.get('job')['count'] == 48


def test_job_path_cannot_escape_store(tmp_path):
    store = JobStore()
    store.jobs_dir = tmp_path
    assert store.get('../outside') is None
    with pytest.raises(ValueError):
        store.set('../outside', {})
