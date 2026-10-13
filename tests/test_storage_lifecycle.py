import time
import shutil
import subprocess
import json
import uuid
from pathlib import Path

import pytest

from app.config import config
from app.services.heavy_gate import HeavyGate, ProcessingBusy
from app.services.job_store import job_store
from app.services.source_store import source_store
from app.services.storage_lifecycle import StorageLifecycleManager, InsufficientStorage


@pytest.fixture
def storage(tmp_path, monkeypatch):
    for name in ("input", "output", "temp", "jobs", "sources/registry",
                 "sources/trends/youtube", "youtube", "config"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(job_store, "jobs_dir", tmp_path / "jobs")
    monkeypatch.setattr(source_store, "registry_dir", tmp_path / "sources/registry")
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "color=c=blue:s=32x32:r=10", "-f", "lavfi", "-i",
         "anullsrc=r=44100:cl=mono", "-t", "0.4", "-c:v", "mpeg4",
         "-c:a", "aac", "-shortest", str(tmp_path / "sample.mp4")],
        check=True, capture_output=True)
    return StorageLifecycleManager(tmp_path)


def job(storage, *, status="COMPLETED", published=False, age=0):
    root = storage.root
    source = root / "input/job_test_source.mp4"
    source.write_bytes(b"source")
    renders = []
    for rank in (1, 2):
        output = root / "output" / f"job_test_clip_{rank:02d}_sample.mp4"
        shutil.copy(storage.root / "sample.mp4", output)
        renders.append({"rank": rank, "success": True, "output_path": str(output),
                        "completed_at": time.time() - age,
                        "publish_status": "UPLOADED" if published and rank == 1 else None,
                        "youtube_video_id": "abcdefghijk" if published and rank == 1 else None})
    data = {"id": "job_test", "status": status, "filepath": str(source),
            "lifecycle_version": 1, "source_storage_status": "LOCAL",
            "source_cleanup_eligible": True,
            "render_request": {"candidates": [{"rank": 1}, {"rank": 2}]},
            "renders": renders, "failed_at": time.time() - age,
            "completed_at": time.time() - age}
    job_store.set("job_test", data)
    return data


def test_gate_rejects_second_video_and_allows_same_owner():
    gate = HeavyGate()
    gate.claim("first")
    gate.claim("first")
    with pytest.raises(ProcessingBusy):
        gate.claim("second")
    gate.release("second")
    assert gate.occupied_by_other("second")
    gate.release("first")
    gate.claim("second")


def test_source_survives_processing_and_partial_render(storage):
    data = job(storage, status="RENDERING")
    assert not storage.cleanup_source(data["id"])
    assert Path(data["filepath"]).exists()
    data["status"] = "COMPLETED"
    data["renders"][1]["success"] = False
    job_store.set(data["id"], data)
    assert not storage.cleanup_source(data["id"])
    assert Path(data["filepath"]).exists()


def test_corrupt_final_render_keeps_source(storage):
    data = job(storage)
    Path(data["renders"][1]["output_path"]).write_bytes(b"broken")
    assert not storage.cleanup_source(data["id"])
    assert Path(data["filepath"]).exists()


def test_source_deleted_after_all_outputs_and_metadata_retained(storage):
    data = job(storage)
    assert storage.cleanup_source(data["id"])
    saved = job_store.get(data["id"])
    assert saved["filepath"] is None
    assert saved["source_storage_status"] == "CLEANED"
    assert not Path(data["filepath"]).exists()
    assert all(Path(r["output_path"]).exists() for r in saved["renders"])
    assert storage.cleanup_source(data["id"])


def test_youtube_attribution_survives_source_mp4_cleanup(storage):
    from app.models.schemas import SourceAsset
    data = job(storage)
    video_id = "abcdefghijk"
    url = f"https://www.youtube.com/watch?v={video_id}"
    data.update(source_id=f"youtube_{video_id}", source_video_id=video_id,
                source_video_title="Original video", source_channel_name="Original channel",
                source_channel_id="UCoriginal", source_url=url,
                rights_status="UNKNOWN", rights_evidence="STANDARD_YOUTUBE_LICENSE")
    for render in data["renders"]:
        render.update(source_video_id=video_id, source_video_title="Original video",
                      source_channel_name="Original channel", source_channel_id="UCoriginal",
                      source_url=url, rights_status="UNKNOWN",
                      rights_evidence="STANDARD_YOUTUBE_LICENSE",
                      youtube_metadata={"description": f"⚠️ Copyright & Source Notice:\n🔗 Source: {url}",
                                        "source_video_id": video_id, "source_url": url})
    job_store.set(data["id"], data)
    source_store.set(SourceAsset(
        source_id=f"youtube_{video_id}", provider="youtube", provider_video_id=video_id,
        title="Original video", source_video_id=video_id, source_video_title="Original video",
        source_channel_name="Original channel", source_channel_id="UCoriginal",
        source_url=url, source_path=data["filepath"], duration=1, width=32, height=32,
        video_codec="mpeg4", audio_codec="aac", rights_status="UNKNOWN",
        rights_evidence="STANDARD_YOUTUBE_LICENSE", acquisition_status="COMPLETED",
        processing_status="READY", created_at=1))
    assert storage.cleanup_source(data["id"])
    saved_job = job_store.get(data["id"])
    saved_asset = source_store.get(data["source_id"])
    assert not Path(data["filepath"]).exists()
    assert saved_job["source_video_id"] == saved_asset.source_video_id == video_id
    assert saved_job["source_channel_id"] == saved_asset.source_channel_id == "UCoriginal"
    assert saved_asset.source_path == ""
    assert saved_job["renders"][0]["youtube_metadata"]["source_url"] == url
    assert saved_job["renders"][0]["rights_evidence"] == "STANDARD_YOUTUBE_LICENSE"


def test_source_cleanup_accepts_already_published_and_cleaned_clip(storage):
    data = job(storage)
    Path(data["renders"][0]["output_path"]).unlink()
    data["renders"][0].update(output_path=None, storage_status="CLEANED",
                               publish_status="UPLOADED", youtube_video_id="abcdefghijk")
    job_store.set(data["id"], data)
    assert storage.cleanup_source(data["id"])
    assert not Path(data["filepath"]).exists()
    saved = job_store.get(data["id"])
    assert saved["filepath"] is None
    assert saved["source_storage_status"] == "CLEANED"
    assert storage.cleanup_source(data["id"])


def test_published_cleanup_is_per_render_and_requires_confirmation(storage):
    data = job(storage)
    assert not storage.cleanup_render(data["id"], 1)
    data["renders"][0]["publish_status"] = "UPLOADED"
    data["renders"][0]["youtube_video_id"] = "abcdefghijk"
    job_store.set(data["id"], data)
    assert storage.cleanup_render(data["id"], 1)
    saved = job_store.get(data["id"])
    assert saved["renders"][0]["output_path"] is None
    assert saved["renders"][0]["storage_status"] == "CLEANED"
    assert Path(saved["renders"][1]["output_path"]).exists()
    assert storage.cleanup_render(data["id"], 1)


def test_expired_output_cleanup_keeps_recent_and_active(storage, monkeypatch):
    monkeypatch.setattr(config, "FINAL_OUTPUT_RETENTION_HOURS", 72)
    data = job(storage, age=71 * 3600)
    assert storage.cleanup_expired_outputs() == 0
    data = job(storage, age=73 * 3600)
    data["status"] = "RENDERING"
    job_store.set(data["id"], data)
    assert storage.cleanup_expired_outputs() == 0
    data["status"] = "COMPLETED"
    job_store.set(data["id"], data)
    assert storage.cleanup_expired_outputs() == 2
    assert all(r["storage_status"] == "CLEANED" for r in job_store.get(data["id"])["renders"])


def test_expired_output_cleanup_waits_for_active_bulk_publish(storage):
    data = job(storage, age=73 * 3600)
    data["bulk_publish_status"] = "RUNNING"
    data["bulk_publish_started_at"] = time.time()
    job_store.set(data["id"], data)
    assert storage.cleanup_expired_outputs() == 0
    assert all(Path(r["output_path"]).exists() for r in data["renders"])
    data["bulk_publish_status"] = "IDLE"
    job_store.set(data["id"], data)
    assert storage.cleanup_expired_outputs() == 2


def test_failed_job_retention_and_active_protection(storage, monkeypatch):
    monkeypatch.setattr(config, "FAILED_JOB_RETENTION_HOURS", 24)
    data = job(storage, status="FAILED", age=23 * 3600)
    assert storage.cleanup_failed_jobs() == 0
    data = job(storage, status="RENDERING", age=25 * 3600)
    assert storage.cleanup_failed_jobs() == 0
    data["status"] = "FAILED"
    job_store.set(data["id"], data)
    assert storage.cleanup_failed_jobs() == 3
    saved = job_store.get(data["id"])
    assert saved["filepath"] is None
    assert all(r["output_path"] is None for r in saved["renders"])


def test_new_video_preflight_applies_failed_retention_without_touching_oauth(storage):
    data = job(storage, status="FAILED", age=25 * 3600)
    token = storage.root / "youtube/token.json"
    token.write_text("protected")
    result = storage.prepare_for_new_video()
    assert result["failed_files"] == 3
    assert job_store.get(data["id"])["source_storage_status"] == "CLEANED"
    assert all(render["storage_status"] == "CLEANED"
               for render in job_store.get(data["id"])["renders"])
    assert token.read_text() == "protected"


def test_explicit_failed_publish_discard_is_exact_and_idempotent(storage):
    data = job(storage)
    source = Path(data["filepath"])
    source.unlink()
    data["filepath"] = None
    data["source_storage_status"] = "CLEANED"
    for render in data["renders"]:
        render.update(storage_status="LOCAL", publish_status="FAILED",
                      publish_error_code="QUOTA_EXCEEDED")
    job_store.set(data["id"], data)
    token = storage.root / "youtube/token.json"
    token.write_text("protected")
    outputs = [Path(render["output_path"]) for render in data["renders"]]

    assert storage.discard_failed_publication_renders(
        "job_test", (1,), {"QUOTA_EXCEEDED"})["deleted_files"] == 0
    assert all(path.exists() for path in outputs)
    result = storage.discard_failed_publication_renders(
        "job_test", (1, 2), {"QUOTA_EXCEEDED"})
    assert result["deleted_files"] == 2
    assert result["reclaimed_bytes"] > 0
    assert all(not path.exists() for path in outputs)
    assert token.read_text() == "protected"
    saved = job_store.get("job_test")
    assert all(render["storage_status"] == "CLEANED" and
               render["cleanup_reason"] == "USER_DISCARDED" and
               render["output_path"] is None for render in saved["renders"])
    assert storage.discard_failed_publication_renders(
        "job_test", (1, 2), {"QUOTA_EXCEEDED"})["deleted_files"] == 0


def test_explicit_discard_preserves_published_or_shared_render(storage):
    data = job(storage)
    data["source_storage_status"] = "CLEANED"
    for render in data["renders"]:
        render.update(storage_status="LOCAL", publish_status="FAILED",
                      publish_error_code="QUOTA_EXCEEDED")
    data["renders"][1]["youtube_video_id"] = "publishedid"
    job_store.set(data["id"], data)
    outputs = [Path(render["output_path"]) for render in data["renders"]]
    assert storage.discard_failed_publication_renders(
        "job_test", (1, 2), {"QUOTA_EXCEEDED"})["deleted_files"] == 0
    assert all(path.exists() for path in outputs)

    data["renders"][1]["youtube_video_id"] = None
    job_store.set("job_test", data)
    job_store.set("other_job", {"id": "other_job", "status": "COMPLETED",
                                "renders": [{"output_path": str(outputs[0])}]})
    assert storage.discard_failed_publication_renders(
        "job_test", (1, 2), {"QUOTA_EXCEEDED"})["deleted_files"] == 0
    assert all(path.exists() for path in outputs)


@pytest.mark.parametrize("relative", ["youtube/token.json", "config/key.json",
                                      "input/../../outside.mp4"])
def test_protected_and_traversal_paths_are_rejected(storage, tmp_path, relative):
    path = (tmp_path / "input/../../" / f"{tmp_path.name}_outside.mp4"
            if relative.startswith("input/../../") else tmp_path / relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"keep")
    with pytest.raises(ValueError, match="UNSAFE_STORAGE_PATH"):
        storage._delete(path, tmp_path / "input", tmp_path / "youtube", tmp_path / "config")
    assert path.exists()
    if not path.resolve().is_relative_to(tmp_path.resolve()):
        path.unlink()


def test_symlink_escape_is_rejected(storage, tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}_outside.mp4"
    outside.write_bytes(b"keep")
    link = tmp_path / "input/link.mp4"
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="UNSAFE_STORAGE_PATH"):
        storage._delete(link, tmp_path / "input")
    assert outside.exists()
    outside.unlink()


def test_symlinked_parent_is_rejected(storage):
    real_dir = storage.root / "input/real"
    real_dir.mkdir()
    real_file = real_dir / "source.mp4"
    real_file.write_bytes(b"keep")
    alias = storage.root / "input/alias"
    alias.symlink_to(real_dir, target_is_directory=True)
    with pytest.raises(ValueError, match="UNSAFE_STORAGE_PATH"):
        storage._delete(alias / "source.mp4", storage.root / "input")
    assert real_file.exists()


def test_cleanup_rejects_nonmedia_paths_even_inside_data_root(storage):
    acquisition = storage.root / "sources/trends/youtube/abcdefghijk/acquisition.json"
    acquisition.parent.mkdir(parents=True)
    acquisition.write_text("{}")
    with pytest.raises(ValueError, match="UNSAFE_STORAGE_PATH"):
        storage._source_file(acquisition, "job_test")
    metadata = storage.root / "output/metadata.json"
    metadata.write_text("{}")
    with pytest.raises(ValueError, match="UNSAFE_STORAGE_PATH"):
        storage._render_file(metadata, "job_test", 1)
    assert acquisition.exists() and metadata.exists()


def test_cleanup_rejects_another_jobs_media_inside_data_root(storage):
    other_source = storage.root / "input/other_job_source.mp4"
    other_render = storage.root / "output/other_job_clip_01_sample.mp4"
    other_source.write_bytes(b"keep")
    other_render.write_bytes(b"keep")
    with pytest.raises(ValueError, match="UNSAFE_STORAGE_PATH"):
        storage._source_file(other_source, "job_test")
    with pytest.raises(ValueError, match="UNSAFE_STORAGE_PATH"):
        storage._render_file(other_render, "job_test", 1)
    assert other_source.exists() and other_render.exists()


def test_disk_guard(storage, monkeypatch):
    monkeypatch.setattr(config, "MIN_FREE_DISK_GB", 1.0)
    monkeypatch.setattr("app.services.storage_lifecycle.shutil.disk_usage",
                        lambda _: (5 * 1024 ** 3, 4.5 * 1024 ** 3, 0.5 * 1024 ** 3))
    with pytest.raises(InsufficientStorage):
        storage.require_space()


def test_storage_inventory_is_aggregate_and_read_only(storage, monkeypatch):
    from app.services import storage_inventory as inventory_module
    monkeypatch.setattr(inventory_module.storage_lifecycle, "root", storage.root)
    token = storage.root / "youtube/token.json"
    token.write_bytes(b"secret-value")
    unknown = storage.root / "input/legacy.mp4"
    unknown.write_bytes(b"legacy-bytes")
    result = inventory_module.storage_inventory()
    assert result["categories"]["youtube"]["bytes"] == len(b"secret-value")
    assert result["roles"]["protected"]["files"] == 1
    assert result["roles"]["unclassified"]["bytes"] >= len(b"legacy-bytes")
    assert isinstance(result["acquisition_preflight_ok"], bool)
    assert "token.json" not in str(result)
    assert "secret-value" not in str(result)
    assert token.exists() and unknown.exists()


def test_retention_dry_run_uses_mtime_only_for_known_final(storage, monkeypatch):
    from app.services import storage_inventory as inventory_module
    import os
    monkeypatch.setattr(inventory_module.storage_lifecycle, "root", storage.root)
    data = job(storage)
    data.pop("completed_at")
    for render in data["renders"]:
        render.pop("completed_at")
    job_store.set(data["id"], data)
    old = time.time() - 73 * 3600
    os.utime(data["renders"][0]["output_path"], (old, old))
    result = inventory_module.storage_inventory()["expired_unpublished_final_dry_run"]
    assert result["files"] == 1
    assert result["missing_timestamps_files"] == 2
    assert Path(data["renders"][0]["output_path"]).exists()


def test_intermediate_cleanup_is_scoped(storage):
    mine = storage.root / "temp/job_test_proxy.mp4"
    other = storage.root / "temp/job_other_proxy.mp4"
    render_temp = storage.root / "output/temp_job_test_clip_01_part0.mp4"
    other_render_temp = storage.root / "output/temp_job_other_clip_01_part0.mp4"
    mine.write_bytes(b"x")
    other.write_bytes(b"y")
    render_temp.write_bytes(b"x")
    other_render_temp.write_bytes(b"y")
    storage.cleanup_job_intermediates("job_test")
    assert not mine.exists()
    assert other.exists()
    assert not render_temp.exists()
    assert other_render_temp.exists()
    storage.cleanup_job_intermediates("../*")
    assert other.exists()


def test_stale_heavy_job_is_requeued_after_restart(storage):
    from app.services.worker import PipelineWorker
    data = job(storage, status="RENDERING")
    worker = PipelineWorker()
    worker._recover_jobs()
    assert job_store.get(data["id"])["status"] == "QUEUED_RENDER"


def test_old_production_metadata_is_not_automatically_cleaned(storage):
    data = job(storage, status="FAILED", age=100 * 3600)
    data.pop("lifecycle_version")
    job_store.set(data["id"], data)
    assert storage.cleanup_failed_jobs() == 0
    assert storage.cleanup_expired_outputs() == 0
    assert Path(data["filepath"]).exists()


def test_legacy_reconciliation_requires_all_valid_renders(storage):
    data = job(storage)
    data.pop("lifecycle_version")
    data.pop("source_cleanup_eligible")
    job_store.set(data["id"], data)
    actions = storage.plan_reconciliation()
    assert any(a["kind"] == "source" and a["job_id"] == data["id"] for a in actions)
    assert Path(data["filepath"]).exists()
    Path(data["renders"][0]["output_path"]).write_bytes(b"corrupt")
    assert not any(a["kind"] == "source" for a in storage.plan_reconciliation())


def test_reconciliation_keeps_failed_publication_and_recent_outputs(storage):
    data = job(storage, age=100 * 3600)
    data.pop("lifecycle_version")
    data["renders"][0]["publish_status"] = "FAILED"
    data["renders"][1]["completed_at"] = time.time()
    job_store.set(data["id"], data)
    assert not any(a["kind"] in ("uploaded", "expired")
                   for a in storage.plan_reconciliation())
    data["renders"][1]["publish_status"] = "UPLOADED"
    data["renders"][1]["youtube_video_id"] = "abcdefghijk"
    job_store.set(data["id"], data)
    actions = storage.plan_reconciliation()
    assert [(a["kind"], a["rank"]) for a in actions if a["kind"] == "uploaded"] == [
        ("uploaded", 2)]


def test_reconciliation_keeps_active_source_and_ambiguous_legacy(storage):
    data = job(storage, status="RENDERING")
    data.pop("lifecycle_version")
    job_store.set(data["id"], data)
    ambiguous = storage.root / "input/no-owner.mp4"
    ambiguous.write_bytes(b"keep")
    assert not any(a["kind"] == "source" for a in storage.plan_reconciliation())
    assert ambiguous.exists()


def test_reconciliation_deletes_only_valid_legacy_source(storage):
    data = job(storage)
    data.pop("lifecycle_version")
    data.pop("source_cleanup_eligible")
    job_store.set(data["id"], data)
    before = Path(data["filepath"]).stat().st_size
    assert storage.reconcile_storage()["reclaimed_bytes"] >= before
    assert not Path(data["filepath"]).exists()
    assert job_store.get(data["id"])["source_storage_status"] == "CLEANED"
    assert storage.reconcile_storage()["deleted_files"] == 0


def test_reconciliation_preserves_oauth_and_config(storage):
    token = storage.root / "youtube/token.json"
    token.write_bytes(b"oauth")
    secret = storage.root / "config/secret.json"
    secret.write_bytes(b"config")
    job(storage)
    storage.reconcile_storage()
    assert token.read_bytes() == b"oauth"
    assert secret.read_bytes() == b"config"


def test_legacy_youtube_source_marks_registry_cleaned(storage):
    from app.models.schemas import SourceAsset
    data = job(storage)
    video_id = "abcdefghijk"
    new_id = f"job_youtube_{video_id}"
    target = storage.root / "sources/trends/youtube" / video_id / "source.mp4"
    target.parent.mkdir()
    Path(data["filepath"]).rename(target)
    for render in data["renders"]:
        old = Path(render["output_path"])
        renamed = old.with_name(old.name.replace("job_test", new_id))
        old.rename(renamed)
        render["output_path"] = str(renamed)
    (storage.root / "jobs/job_test.json").unlink()
    data.update(id=new_id, filepath=str(target))
    data.pop("lifecycle_version")
    data.pop("source_cleanup_eligible")
    job_store.set(new_id, data)
    source_store.set(SourceAsset(
        source_id=f"youtube_{video_id}", provider="youtube", provider_video_id=video_id,
        title="Legacy", source_path=str(target), duration=1, width=32, height=32,
        video_codec="mpeg4", audio_codec="aac", rights_status="UNKNOWN",
        acquisition_status="COMPLETED", processing_status="READY", created_at=1))
    assert storage.cleanup_source(new_id)
    asset = source_store.get(f"youtube_{video_id}")
    assert asset.storage_status == "CLEANED"
    assert asset.source_path == ""
    assert job_store.get(new_id)["filepath"] is None
    assert storage.reconcile_legacy_attribution() == 1
    saved = job_store.get(new_id)
    assert saved["source_video_id"] == video_id
    assert saved["source_video_title"] == "Legacy"
    assert saved["source_url"] == f"https://www.youtube.com/watch?v={video_id}"
    assert all("Copyright & Source Notice" in render["youtube_metadata"]["description"]
               for render in saved["renders"])
    assert storage.reconcile_legacy_attribution() == 0


def test_reconciliation_stale_partial_requires_terminal_record(storage):
    dl_id = str(uuid.uuid4())
    partial = storage.root / "temp/downloads" / dl_id / "1/source.mp4.part"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"partial")
    old = time.time() - 2 * 3600
    import os
    os.utime(partial, (old, old))
    record = storage.root / "sources/trends/youtube/abcdefghijk/acquisition.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({"id": dl_id, "status": "DOWNLOADING"}))
    assert not any(a["kind"] == "partial" for a in storage.plan_reconciliation())
    record.write_text(json.dumps({"id": dl_id, "status": "FAILED"}))
    assert any(a["kind"] == "partial" for a in storage.plan_reconciliation())
    storage.reconcile_storage()
    assert not partial.exists()


def test_failed_publication_survives_expired_output_cleanup(storage):
    data = job(storage, age=100 * 3600)
    data["renders"][0]["publish_status"] = "FAILED"
    job_store.set(data["id"], data)
    assert storage.cleanup_expired_outputs() == 1
    assert Path(data["renders"][0]["output_path"]).exists()
    assert not Path(data["renders"][1]["output_path"]).exists()


def test_exact_unreferenced_duplicate_can_be_reconciled(storage):
    data = job(storage)
    duplicate = storage.root / "input/job_test_duplicate.mp4"
    shutil.copy(data["filepath"], duplicate)
    actions = storage.plan_reconciliation()
    assert any(a["kind"] == "duplicate" and a["path"] == duplicate for a in actions)
    storage.reconcile_storage()
    assert not duplicate.exists()
    assert Path(data["renders"][0]["output_path"]).exists()


def test_acquisition_reserve_adds_to_hard_floor(storage, monkeypatch):
    monkeypatch.setattr(config, "MIN_FREE_DISK_GB", 1.0)
    monkeypatch.setattr(config, "ACQUISITION_WORKING_RESERVE_GB", 0.75)
    monkeypatch.setattr(storage, "get_disk_usage", lambda: {"total": 5 * 1024 ** 3,
        "used": int(3.5 * 1024 ** 3), "free": int(1.5 * 1024 ** 3)})
    storage.require_space()
    with pytest.raises(InsufficientStorage):
        storage.require_acquisition_space()
    monkeypatch.setattr(storage, "get_disk_usage", lambda: {"total": 5 * 1024 ** 3,
        "used": 3 * 1024 ** 3, "free": 2 * 1024 ** 3})
    storage.require_acquisition_space()


def test_direct_upload_budget_allows_one_gb_and_preserves_processing_floor(storage, monkeypatch):
    import app.services.storage_inventory as inventory_module

    monkeypatch.setattr(config, "MIN_FREE_DISK_GB", 1.0)
    monkeypatch.setattr(config, "DIRECT_UPLOAD_MIN_FREE_GB", 0.5)
    monkeypatch.setattr(config, "UPLOAD_WORKING_RESERVE_GB", 0.125)
    monkeypatch.setattr(storage, "get_disk_usage", lambda: {"total": 4838498304,
        "used": 3064414208, "free": 1774084096})
    storage.require_upload_space(1_000_000_000)
    monkeypatch.setattr(inventory_module.storage_lifecycle, "root", storage.root)
    monkeypatch.setattr(inventory_module.storage_lifecycle, "get_disk_usage",
                        storage.get_disk_usage)
    assert inventory_module.storage_inventory()["max_stream_upload_bytes"] >= 1_000_000_000
    with pytest.raises(InsufficientStorage):
        storage.require_upload_space(1_200_000_000)
    direct = {"storage_profile": "DIRECT_UPLOAD"}
    monkeypatch.setattr(storage, "get_disk_usage", lambda: {"total": 4838498304,
        "used": 4064414208, "free": 774084096})
    storage.require_processing_space(direct)
    storage.require_processing_space(direct, 64 * 1024 ** 2)
    with pytest.raises(InsufficientStorage):
        storage.require_space()
    with pytest.raises(InsufficientStorage):
        storage.require_processing_space({"source_id": "youtube_example"})


def test_startup_reconciliation_failure_does_not_block_app(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.services.worker import worker
    from app.services.scheduler import scheduler
    from app.services.download_service import download_service
    from app.services.storage_lifecycle import storage_lifecycle

    def fail():
        raise RuntimeError("transient")

    monkeypatch.setattr(download_service, "reconcile_interrupted", fail)
    monkeypatch.setattr(storage_lifecycle, "reconcile_storage", fail)
    monkeypatch.setattr(download_service, "stop", lambda: None)
    monkeypatch.setattr(worker, "start", lambda: None)
    monkeypatch.setattr(worker, "stop", lambda: None)
    monkeypatch.setattr(scheduler, "start", lambda: None)
    monkeypatch.setattr(scheduler, "stop", lambda: None)
    with TestClient(app) as client:
        assert client.get("/").status_code == 200


def test_existing_source_is_preserved_when_new_job_processes_it(storage):
    data = job(storage, age=100 * 3600)
    data["source_cleanup_eligible"] = False
    job_store.set(data["id"], data)
    assert not storage.cleanup_source(data["id"])
    assert Path(data["filepath"]).exists()
    data["status"] = "FAILED"
    job_store.set(data["id"], data)
    storage.cleanup_failed_jobs()
    assert Path(data["filepath"]).exists()


def test_library_omits_cleaned_and_missing_sources(storage, monkeypatch):
    from app.api.routes import get_sources
    from app.models.schemas import SourceAsset
    local = storage.root / "input/local.mp4"
    local.write_bytes(b"video")
    def asset(source_id, path, storage_status):
        return SourceAsset(
            source_id=source_id, provider="youtube", provider_video_id="abcdefghijk",
            title=source_id, source_path=str(path), duration=1, width=1, height=1,
            video_codec="h264", audio_codec="aac", rights_status="LICENSED",
            acquisition_status="COMPLETED", processing_status="READY", created_at=0,
            storage_status=storage_status)
    monkeypatch.setattr(source_store, "get_all", lambda: [
        asset("local", local, "LOCAL"),
        asset("cleaned", local, "CLEANED"),
        asset("missing", storage.root / "input/missing.mp4", "LOCAL")])
    result = get_sources()["sources"]
    assert [source["source_id"] for source in result] == ["local"]
    assert "source_path" not in result[0]
