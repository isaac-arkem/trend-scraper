import os
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

_client: Client | None = None


def get_db() -> Client:
    global _client
    if _client is None:
        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_SECRET_KEY"]
        _client = create_client(url, key)
    return _client


def upsert(table: str, data: dict | list, on_conflict: str = None) -> list:
    db = get_db()
    kwargs = {}
    if on_conflict:
        kwargs["on_conflict"] = on_conflict
    res = db.table(table).upsert(data, **kwargs).execute()
    return res.data


def insert(table: str, data: dict | list) -> list:
    db = get_db()
    res = db.table(table).insert(data).execute()
    return res.data


def select(table: str, filters: dict = None, columns: str = "*", limit: int = None) -> list:
    db = get_db()
    query = db.table(table).select(columns)
    if filters:
        for col, val in filters.items():
            query = query.eq(col, val)
    if limit:
        query = query.limit(limit)
    res = query.execute()
    return res.data


def update(table: str, match: dict, data: dict) -> list:
    db = get_db()
    query = db.table(table).update(data)
    for col, val in match.items():
        query = query.eq(col, val)
    res = query.execute()
    return res.data


def rpc(fn: str, params: dict = None) -> any:
    db = get_db()
    res = get_db().rpc(fn, params or {}).execute()
    return res.data
