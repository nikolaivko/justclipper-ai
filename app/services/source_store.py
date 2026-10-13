import json
import re
from pathlib import Path
from typing import Dict, Any, List
from app.config import config
from app.models.schemas import SourceAsset
from app.services.json_storage import atomic_write_json, record_lock

class SourceStore:
    def __init__(self):
        self.registry_dir = config.DATA_ROOT / "sources" / "registry"
        self.registry_dir.mkdir(parents=True, exist_ok=True)
        
    def _get_path(self, source_id: str) -> Path:
        if not isinstance(source_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", source_id):
            raise ValueError("INVALID_SOURCE_ID")
        return self.registry_dir / f"{source_id}.json"

    @staticmethod
    def _asset_from_record(data: dict) -> SourceAsset:
        # Old YouTube acquisitions reached storage only after a live CC license check.
        if (data.get("provider") == "youtube" and
                data.get("rights_status") == "CREATIVE_COMMONS_OR_COMPATIBLE"):
            data = {**data, "rights_status": "LICENSED"}
        return SourceAsset(**data)
        
    def get(self, source_id: str) -> SourceAsset:
        try:
            data = json.loads(self._get_path(source_id).read_text())
            return self._asset_from_record(data) if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None
            
    def set(self, source_asset: SourceAsset):
        p = self._get_path(source_asset.source_id)
        with record_lock(p):
            atomic_write_json(p, source_asset.model_dump())
            
    def get_all(self) -> List[SourceAsset]:
        results = []
        for p in self.registry_dir.glob("*.json"):
            asset = self.get(p.stem)
            if asset is not None:
                results.append(asset)
        return sorted(results, key=lambda x: x.created_at, reverse=True)

source_store = SourceStore()
