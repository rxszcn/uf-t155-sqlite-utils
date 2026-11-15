import datetime

import pytest

from sqlite_utils.db import NoTable
from sqlite_utils.utils import sqlite3


def test_duplicate(fresh_db):
    # Create table using native Sqlite statement:
    fresh_db.execute("""CREATE TABLE "table1" (
    "text_col" TEXT,
    "real_col" REAL,
    "int_col" INTEGER,
    "bool_col" INTEGER,
    "datetime_col" TEXT)""")
    # Insert one row of mock data:
    dt = datetime.datetime.now(datetime.timezone.utc)
    data = {
        "text_col": "Cleo",
        "real_col": 3.14,
        "int_col": -255,
        "bool_col": True,
        "datetime_col": str(dt),
    }
    table1 = fresh_db.table("table1")
    row_id = table1.insert(data).last_rowid
    # Duplicate table:
    table2 = table1.duplicate("table2")
    # Ensure data integrity:
    assert data == table2.get(row_id)
    # Ensure schema integrity:
    assert [
        {"name": "text_col", "type": "TEXT"},
        {"name": "real_col", "type": "REAL"},
        {"name": "int_col", "type": "INT"},
        {"name": "bool_col", "type": "INT"},
        {"name": "datetime_col", "type": "TEXT"},
    ] == [{"name": col.name, "type": col.type} for col in table2.columns]


def test_duplicate_fails_if_table_does_not_exist(fresh_db):
    with pytest.raises(NoTable):
        fresh_db.table("not_a_table").duplicate("duplicated")


@pytest.fixture
def structured_db(fresh_db):
    # A table exercising every structural component duplicate() can copy
    fresh_db.execute("""
        CREATE TABLE "items" (
            "id" INTEGER PRIMARY KEY,
            "sku" TEXT NOT NULL UNIQUE,
            "name" TEXT NOT NULL,
            "price" REAL CHECK (price >= 0),
            "parent_id" INTEGER REFERENCES "items" ("id")
        )
        """)
    fresh_db.execute('CREATE INDEX "idx_items_name" ON "items" ("name")')
    fresh_db.execute("""
        CREATE TRIGGER "items_price_guard" AFTER INSERT ON "items" BEGIN
            UPDATE "items" SET price = 0 WHERE id = new.id AND new.price IS NULL;
        END
        """)
    fresh_db["items"].insert_all(
        [
            {"id": 1, "sku": "a-1", "name": "apple", "price": 1.5},
            {"id": 2, "sku": "b-2", "name": "banana", "price": 0.5},
        ]
    )
    return fresh_db


def test_duplicate_default_stays_lightweight(structured_db):
    # Without structure=True the copy keeps columns, types and rows only
    copy = structured_db["items"].duplicate("items_copy")
    assert copy.count == 2
    assert structured_db["items"].columns_dict == copy.columns_dict
    assert copy.pks == ["rowid"]
    assert copy.indexes == []
    assert copy.triggers == []
    assert copy.detect_fts() is None
    # No NOT NULL constraint on the lightweight copy
    copy.insert({"id": 100, "sku": None, "name": None, "price": None})


def test_duplicate_structure_preserves_column_types(structured_db):
    copy = structured_db["items"].duplicate("items_copy", structure=True)
    assert structured_db["items"].columns_dict == copy.columns_dict
    assert copy.count == 2
    assert copy.get(1)["name"] == "apple"


def test_duplicate_structure_preserves_primary_key(structured_db):
    copy = structured_db["items"].duplicate("items_copy", structure=True)
    assert copy.pks == ["id"]
    with pytest.raises(sqlite3.IntegrityError):
        copy.insert({"id": 1, "sku": "x-9", "name": "dupe", "price": 1})


def test_duplicate_structure_preserves_unique_constraint(structured_db):
    copy = structured_db["items"].duplicate("items_copy", structure=True)
    with pytest.raises(sqlite3.IntegrityError):
        copy.insert({"id": 100, "sku": "a-1", "name": "dupe", "price": 1})


def test_duplicate_structure_preserves_not_null(structured_db):
    copy = structured_db["items"].duplicate("items_copy", structure=True)
    with pytest.raises(sqlite3.IntegrityError):
        copy.insert({"id": 100, "sku": "x-9", "name": None, "price": 1})


def test_duplicate_structure_preserves_check_constraint(structured_db):
    copy = structured_db["items"].duplicate("items_copy", structure=True)
    with pytest.raises(sqlite3.IntegrityError):
        copy.insert({"id": 100, "sku": "x-9", "name": "bad", "price": -1})


def test_duplicate_structure_rebinds_self_referencing_foreign_key(structured_db):
    structured_db["items"].duplicate("items_copy", structure=True)
    create_sql = structured_db["items_copy"].schema
    assert 'REFERENCES "items_copy"' in create_sql
    assert 'REFERENCES "items"' not in create_sql


def test_duplicate_structure_copies_indexes(structured_db):
    copy = structured_db["items"].duplicate("items_copy", structure=True)
    index_names = {index.name for index in copy.indexes}
    assert "idx_items_copy_name" in index_names
    index_sql = fresh_index_sql(structured_db, "idx_items_copy_name")
    assert '"items_copy"' in index_sql
    assert '"items"' not in index_sql
    # Index names are a database-wide namespace: the original is untouched
    assert "idx_items_name" in {index.name for index in structured_db["items"].indexes}


def fresh_index_sql(db, name):
    return db.execute(
        "select sql from sqlite_master where type = 'index' and name = ?", (name,)
    ).fetchone()[0]


def test_duplicate_structure_copies_and_rebinds_triggers(structured_db):
    copy = structured_db["items"].duplicate("items_copy", structure=True)
    trigger_names = {trigger.name for trigger in copy.triggers}
    assert "items_copy_price_guard" in trigger_names
    trigger_sql = structured_db["items_copy"].triggers_dict["items_copy_price_guard"]
    assert 'ON "items_copy"' in trigger_sql
    assert 'UPDATE "items_copy"' in trigger_sql
    assert '"items"' not in trigger_sql
    # Trigger fires against the new table, not the old one
    copy.insert({"id": 50, "sku": "c-3", "name": "cherry", "price": None})
    assert copy.get(50)["price"] == 0
    assert structured_db["items"].count == 2


def test_duplicate_structure_index_name_conflict_gets_numeric_suffix(structured_db):
    # Pre-create an index holding the name the copy would naturally take
    structured_db.execute('CREATE INDEX "idx_items_copy_name" ON "items" ("price")')
    copy = structured_db["items"].duplicate("items_copy", structure=True)
    index_names = {index.name for index in copy.indexes}
    assert "idx_items_copy_name_2" in index_names
    index_sql = fresh_index_sql(structured_db, "idx_items_copy_name_2")
    assert '"items_copy"' in index_sql


def test_duplicate_structure_copies_fts(fresh_db):
    table = fresh_db.table("docs")
    table.insert_all(
        [
            {"id": 1, "title": "tanuki are running tricksters"},
            {"id": 2, "title": "racoons are biting trash pandas"},
        ],
        pk="id",
    )
    table.enable_fts(["title"], create_triggers=True)
    copy = table.duplicate("docs_copy", structure=True)
    # FTS virtual table exists against the new table and is searchable
    assert copy.detect_fts() == "docs_copy_fts"
    assert [row["title"] for row in copy.search("tanuki")] == [
        "tanuki are running tricksters"
    ]
    # Synchronization triggers were recreated against the new table
    trigger_names = {trigger.name for trigger in copy.triggers}
    assert {"docs_copy_ai", "docs_copy_ad", "docs_copy_au"}.issubset(trigger_names)
    copy.insert({"id": 3, "title": "tanuki are back"})
    assert {row["title"] for row in copy.search("tanuki")} == {
        "tanuki are running tricksters",
        "tanuki are back",
    }
    # The original table and its FTS index are unaffected
    assert table.count == 2
    assert len(list(table.search("tanuki"))) == 1


def test_duplicate_structure_copies_fts_tokenizer(fresh_db):
    table = fresh_db.table("docs")
    table.insert_all([{"id": 1, "title": "running quickly"}], pk="id")
    table.enable_fts(["title"], tokenize="porter")
    copy = table.duplicate("docs_copy", structure=True)
    assert "porter" in fresh_db["docs_copy_fts"].schema
    # Porter stemming matches "runs" against "running"
    assert len(list(copy.search("runs"))) == 1


def test_duplicate_structure_without_fts_leaves_no_fts(structured_db):
    copy = structured_db["items"].duplicate("items_copy", structure=True)
    assert copy.detect_fts() is None
    assert "items_copy_fts" not in structured_db.table_names()
