"""Public Mongo-compatible palace adapter.

The implementation remains in ``documentdb_palace`` for import compatibility
with existing deployments and migration tooling.
"""

from .documentdb_palace import (  # noqa: F401
    close,
    create_drawer,
    delete_drawer,
    diary_read,
    diary_write,
    get_drawer,
    import_drawer,
    import_kg,
    kg_add,
    kg_invalidate,
    kg_query_rows,
    list_drawers,
    mine_directory,
    search_data,
    search_markdown,
    taxonomy_data,
    update_drawer,
    upsert_drawer,
    wake_up_text,
)
