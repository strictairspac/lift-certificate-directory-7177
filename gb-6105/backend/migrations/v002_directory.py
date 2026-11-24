from src.models import directory  # Register the new tables.
from src.models.base import metadata


def upgrade(connection):
    # Version 2 is additive: online versioned directory, change stream and probe batches.
    metadata.create_all(connection)
