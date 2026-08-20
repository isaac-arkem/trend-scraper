import os
import threading
from typing import Union
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

# Thread-local client: the Supabase client wraps a single httpx.Client whose
# connection pool is not safe to share across many worker threads. Each thread
# gets its own client so the parallel Stage 5 driver doesn't exhaust sockets.
_local = threading.local()


def get_db() -> Client:
    client = getattr(_local, "client", None)
    if client is None:
        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_SECRET_KEY"]
        client = create_client(url, key)
        _local.client = client
    return client


def upsert(table: str, data: Union[dict, list], on_conflict: str = None) -> list:
    db = get_db()
    kwargs = {}
    if on_conflict:
        kwargs["on_conflict"] = on_conflict
    res = db.table(table).upsert(data, **kwargs).execute()
    return res.data


def insert(table: str, data: Union[dict, list]) -> list:
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
