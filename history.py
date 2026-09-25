"""Local durable text history. No audio, API keys or signed source URLs."""
import json
import os
from pathlib import Path
import sqlite3
from datetime import datetime, timezone
import secrets
from contextlib import contextmanager

DB_PATH = Path(os.environ.get('DATA_DIR') or Path(__file__).resolve().parent / 'data') / 'history.sqlite3'


@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.executescript('''
    CREATE TABLE IF NOT EXISTS transcripts (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL,
        result TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS versions (
        id TEXT PRIMARY KEY, transcript_id TEXT NOT NULL, kind TEXT NOT NULL,
        base_id TEXT, created_at TEXT NOT NULL, content TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS versions_parent ON versions(transcript_id, created_at);
    ''')
    os.chmod(DB_PATH, 0o600)
    try:
        with db:
            yield db
    finally:
        db.close()


def now():
    return datetime.now(timezone.utc).isoformat()


def save(key, title, result):
    if not result.get('original'): return
    with connect() as db:
        db.execute('INSERT INTO transcripts VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET result=excluded.result',
                   (key, title[:200], now(), json.dumps(result, ensure_ascii=False)))


def get(key):
    with connect() as db:
        row = db.execute('SELECT * FROM transcripts WHERE id=?', (key,)).fetchone()
        if not row: return None
        versions = db.execute('SELECT * FROM versions WHERE transcript_id=? ORDER BY created_at, id', (key,)).fetchall()
    return {**dict(row), 'result': json.loads(row['result']), 'versions': [dict(v) for v in versions]}


def listing(offset=0):
    with connect() as db:
        total = db.execute('SELECT COUNT(*) FROM transcripts').fetchone()[0]
        rows = db.execute('SELECT * FROM transcripts ORDER BY created_at DESC,id DESC LIMIT 20 OFFSET ?', (offset,)).fetchall()
    items=[]
    for row in rows:
        value=json.loads(row['result'])
        items.append({'id':row['id'], 'title':row['title'], 'created_at':row['created_at'],
                      'language':value.get('language','Idioma ainda não identificado'), 'partial':value.get('partial',False),
                      'preview':(value.get('translation') or value['original'])[:160]})
    return {'items':items,'total':total,'next_offset':offset+len(items) if offset+len(items)<total else None}


def add_version(key, kind, base_id, content):
    version={'id':secrets.token_urlsafe(18),'transcript_id':key,'kind':kind,'base_id':base_id,'created_at':now(),'content':content}
    with connect() as db:
        db.execute('INSERT INTO versions VALUES (:id,:transcript_id,:kind,:base_id,:created_at,:content)',version)
    return version
