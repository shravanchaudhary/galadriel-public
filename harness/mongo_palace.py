"""Public palace adapter.

The implementation lives in ``documentdb_palace``; this module is the name the
harness imports so call sites do not depend on the storage module's filename.
"""

from .documentdb_palace import (  # noqa: F401
    FilterError,
    build_filter,
    close,
    create_drawer,
    delete_drawer,
    diary_read,
    diary_write,
    fetch_data,
    get_drawer,
    kg_add,
    kg_invalidate,
    kg_query_rows,
    list_drawers,
    mine_directory,
    search_data,
    search_markdown,
    segment_text,
    taxonomy_data,
    update_drawer,
    upsert_drawer,
    wake_up_text,
)
