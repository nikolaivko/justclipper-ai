from app.services.storage_lifecycle import storage_lifecycle


def free_disk_space(job: dict | None = None):
    """Legacy entry point: enforce the guard without deleting unknown files."""
    storage_lifecycle.require_processing_space(job)
