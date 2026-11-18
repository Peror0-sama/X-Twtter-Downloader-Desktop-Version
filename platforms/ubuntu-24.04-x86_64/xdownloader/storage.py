from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .core import Tweet, TweetURL, filename

HISTORY_SECONDS = 72 * 60 * 60


class Store:
    def __init__(self, base: Path):
        self.base = base.resolve()
        self.default_download = self.base / "download"
        self.data = self.base / "data"
        self.default_download.mkdir(parents=True, exist_ok=True)
        self.data.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.path = self.data / "queue.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tweets (
                    id TEXT PRIMARY KEY, url TEXT NOT NULL, username TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'queued',
                    error TEXT NOT NULL DEFAULT '', error_code TEXT NOT NULL DEFAULT '',
                    auth_mode TEXT NOT NULL DEFAULT 'anonymous', created TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS media (
                    tweet_id TEXT NOT NULL REFERENCES tweets(id), media_id TEXT NOT NULL,
                    seq INTEGER NOT NULL, kind TEXT NOT NULL, url TEXT NOT NULL, ext TEXT NOT NULL,
                    unique_id TEXT NOT NULL UNIQUE, filename TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'queued', error TEXT NOT NULL DEFAULT '',
                    downloaded INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
                    sha256 TEXT NOT NULL DEFAULT '', speed REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY(tweet_id, media_id));
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY, gif_mode TEXT, created REAL NOT NULL);
            """)
            # 原地兼容 1.0/1.1 数据，已有文件、登录和队列均保留。
            columns = {r[1] for r in db.execute("PRAGMA table_info(tweets)")}
            if 'warning' not in columns:
                db.execute("ALTER TABLE tweets ADD COLUMN warning TEXT NOT NULL DEFAULT ''")
            if "batch_id" not in columns:
                db.execute("ALTER TABLE tweets ADD COLUMN batch_id TEXT NOT NULL DEFAULT ''")
            if "download_dir" not in columns:
                db.execute("ALTER TABLE tweets ADD COLUMN download_dir TEXT NOT NULL DEFAULT ''")
            if "finished_at" not in columns:
                db.execute("ALTER TABLE tweets ADD COLUMN finished_at REAL")
                db.execute("UPDATE tweets SET finished_at=? WHERE status IN ('done','failed')", (time.time(),))
            if "source_sha256" not in {r[1] for r in db.execute("PRAGMA table_info(media)")}:
                db.execute("ALTER TABLE media ADD COLUMN source_sha256 TEXT NOT NULL DEFAULT ''")
            media_columns = {r[1] for r in db.execute("PRAGMA table_info(media)")}
            if 'gif_mode' not in media_columns:
                db.execute("ALTER TABLE media ADD COLUMN gif_mode TEXT")
            if 'download_dir' not in media_columns:
                db.execute("ALTER TABLE media ADD COLUMN download_dir TEXT NOT NULL DEFAULT ''")
                db.execute("UPDATE media SET download_dir=COALESCE((SELECT download_dir FROM tweets WHERE id=media.tweet_id),'')")
            if 'download_started' not in media_columns:
                db.execute("ALTER TABLE media ADD COLUMN download_started INTEGER NOT NULL DEFAULT 0")
                db.execute("""UPDATE media SET download_started=1 WHERE status IN ('downloading','converting','done')
                    OR downloaded>0 OR sha256!='' OR source_sha256!=''""")
                for row in db.execute('SELECT * FROM media WHERE download_started=0').fetchall():
                    path = self.media_path(dict(row))
                    if any(p.exists() for p in (path, path.with_name(path.stem+'.source.part'), path.with_name(path.name+'.part'))):
                        db.execute('UPDATE media SET download_started=1 WHERE tweet_id=? AND media_id=?', (row['tweet_id'],row['media_id']))
            if db.execute("SELECT 1 FROM tweets WHERE batch_id='' LIMIT 1").fetchone():
                legacy = uuid.uuid4().hex
                # 老队列延续原先保留 MP4 的行为；新导入批次才询问。
                db.execute("INSERT INTO batches VALUES(?,?,?)", (legacy, "mp4", time.time()))
                db.execute("UPDATE tweets SET batch_id=? WHERE batch_id=''", (legacy,))
            db.execute("UPDATE tweets SET status='queued',error='上次任务中断，等待恢复' WHERE status IN ('resolving','downloading')")
            db.execute("UPDATE media SET status='queued',speed=0 WHERE status IN ('downloading','converting')")
            db.execute("DELETE FROM media WHERE tweet_id IN (SELECT id FROM tweets WHERE status='deleting')")
            db.execute("DELETE FROM tweets WHERE status='deleting'")
        self.clear_history(expired_only=True)

    @contextmanager
    def connect(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=15)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            try:
                with db:
                    yield db
            finally:
                db.close()

    def add_urls(self, urls: list[TweetURL], auth_mode="anonymous", gif_mode=None):
        if gif_mode not in (None, "mp4", "gif", "individual"):
            raise ValueError("Invalid GIF mode")
        self.clear_history(expired_only=True)
        added, duplicate = [], 0
        with self.connect() as db:
            batch_id = uuid.uuid4().hex
            destination = db.execute("SELECT value FROM settings WHERE key='download_dir'").fetchone()
            destination = destination[0] if destination else ''
            db.execute("INSERT INTO batches VALUES(?,?,?)", (batch_id, gif_mode, time.time()))
            for url in urls:
                result = db.execute("INSERT OR IGNORE INTO tweets(id,url,username,auth_mode,batch_id,download_dir) VALUES(?,?,?,?,?,?)", (url.tweet_id, url.url, url.username, auth_mode, batch_id, destination))
                if result.rowcount:
                    added.append(url.tweet_id)
                else:
                    duplicate += 1
            if not added:
                db.execute("DELETE FROM batches WHERE id=?", (batch_id,))
        return added, duplicate

    @property
    def download(self):
        value = self.setting('download_dir')
        return Path(value) if value else self.default_download

    def set_download_directory(self, directory):
        directory = Path(directory).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ('.xdownloader-write-' + uuid.uuid4().hex)
        try:
            with probe.open('xb') as stream:
                stream.write(b'test')
        finally:
            probe.unlink(missing_ok=True)
        value = '' if directory == self.default_download else str(directory)
        # 与 begin_media 使用同一把锁/事务：切换与真正开始下载不可交错。
        with self.connect() as db:
            db.execute("INSERT INTO settings VALUES('download_dir',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (value,))
            db.execute("UPDATE tweets SET download_dir=? WHERE status!='done'", (value,))
            db.execute("UPDATE media SET download_dir=? WHERE download_started=0 AND status!='done'", (value,))

    def begin_media(self, tweet_id, media_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM media WHERE tweet_id=? AND media_id=?', (tweet_id,media_id)).fetchone()
            if row is None:
                return None
            db.execute('UPDATE media SET download_started=1 WHERE tweet_id=? AND media_id=?', (tweet_id,media_id))
            media = dict(row)
            media['download_started'] = 1
            return media

    def download_directory(self, tweet):
        return Path(tweet['download_dir']) if tweet.get('download_dir') else self.default_download

    def media_path(self, media):
        return self.download_directory(media) / media['filename']

    def batch_mode(self, batch_id):
        with self.connect() as db:
            row = db.execute("SELECT gif_mode FROM batches WHERE id=?", (batch_id,)).fetchone()
            return row[0] if row else None

    def choose_batch_format(self, batch_id, mode):
        if mode not in ("mp4", "gif", "individual"):
            raise ValueError("Invalid GIF mode")
        with self.connect() as db:
            # 一个批次只能作出一次选择，避免已开始的文件使用不同策略。
            db.execute("UPDATE batches SET gif_mode=? WHERE id=? AND gif_mode IS NULL", (mode, batch_id))
            db.execute("UPDATE tweets SET status='queued',error='',error_code='' WHERE batch_id=? AND status='waiting_choice'", (batch_id,))

    def wait_for_format(self, tweet_id):
        with self.connect() as db:
            result = db.execute("""UPDATE tweets SET status='waiting_choice',error='等待选择本批次动图保存格式',error_code=''
                WHERE id=? AND (batch_id IN (SELECT id FROM batches WHERE gif_mode IS NULL)
                OR (batch_id IN (SELECT id FROM batches WHERE gif_mode='individual') AND EXISTS(
                    SELECT 1 FROM media m WHERE m.tweet_id=tweets.id AND m.kind='animated_gif'
                    AND m.ext='mp4' AND m.url!='' AND m.status!='done' AND m.gif_mode IS NULL)))""", (tweet_id,))
            return bool(result.rowcount)

    def waiting_individual(self):
        with self.connect() as db:
            tweets = [dict(r) for r in db.execute("""SELECT t.* FROM tweets t JOIN batches b ON b.id=t.batch_id
                WHERE t.status='waiting_choice' AND b.gif_mode='individual' ORDER BY t.created,t.id""")]
            for tweet in tweets:
                tweet['media'] = [dict(r) for r in db.execute("""SELECT * FROM media WHERE tweet_id=?
                    AND kind='animated_gif' AND ext='mp4' AND url!='' AND status!='done' AND gif_mode IS NULL ORDER BY seq""", (tweet['id'],))]
            return [t for t in tweets if t['media']]

    def choose_media_formats(self, tweet_id, formats):
        if any(mode not in ('mp4','gif') for mode in formats.values()):
            raise ValueError('Invalid GIF mode')
        with self.connect() as db:
            row = db.execute("""SELECT 1 FROM tweets t JOIN batches b ON b.id=t.batch_id
                WHERE t.id=? AND t.status='waiting_choice' AND b.gif_mode='individual'""", (tweet_id,)).fetchone()
            if row is None:
                return False
            pending = {r[0] for r in db.execute("""SELECT media_id FROM media WHERE tweet_id=?
                AND kind='animated_gif' AND ext='mp4' AND url!='' AND status!='done' AND gif_mode IS NULL""", (tweet_id,))}
            if set(formats) != pending or not pending:
                raise ValueError('Media choices changed')
            for mid, mode in formats.items():
                db.execute('UPDATE media SET gif_mode=? WHERE tweet_id=? AND media_id=? AND gif_mode IS NULL', (mode,tweet_id,mid))
            db.execute("UPDATE tweets SET status='queued',error='',error_code='' WHERE id=? AND status='waiting_choice'", (tweet_id,))
            return True

    def waiting_batches(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("""SELECT b.id,COUNT(t.id) AS tweet_count FROM batches b JOIN tweets t ON t.batch_id=b.id
                WHERE b.gif_mode IS NULL AND EXISTS(SELECT 1 FROM tweets w WHERE w.batch_id=b.id AND w.status='waiting_choice')
                GROUP BY b.id ORDER BY b.created""")]

    def clear_history(self, expired_only=False, exclude=(), now=None):
        cutoff = (time.time() if now is None else now) - HISTORY_SECONDS
        with self.connect() as db:
            rows = db.execute("SELECT id,finished_at FROM tweets WHERE status IN ('done','failed')").fetchall()
            ids = [(r['id'],) for r in rows if r['id'] not in exclude and
                   (not expired_only or (r['finished_at'] is not None and r['finished_at'] <= cutoff))]
            # 显式先删子记录以兼容旧版外键；绝不删除 download 中的文件。
            db.executemany("DELETE FROM media WHERE tweet_id=?", ids)
            db.executemany("DELETE FROM tweets WHERE id=?", ids)
            db.execute("DELETE FROM batches WHERE id NOT IN (SELECT batch_id FROM tweets)")
            return len(ids)

    def tweet(self, tweet_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM tweets WHERE id=?", (tweet_id,)).fetchone()
            return dict(row) if row else None

    def delete_records(self, tweet_ids, completed=False):
        """只移除指定列表的推文记录；成品和部分文件均不删除。"""
        count = 0
        with self.connect() as db:
            for tweet_id in set(tweet_ids):
                row = db.execute('SELECT status FROM tweets WHERE id=?',(tweet_id,)).fetchone()
                if row is None or (row['status']=='done') != completed:
                    continue
                db.execute('DELETE FROM media WHERE tweet_id=?',(tweet_id,))
                count += db.execute('DELETE FROM tweets WHERE id=?',(tweet_id,)).rowcount
            db.execute('DELETE FROM batches WHERE id NOT IN (SELECT batch_id FROM tweets)')
        return count

    def media(self, tweet_id):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM media WHERE tweet_id=? ORDER BY seq", (tweet_id,))]

    def snapshot(self):
        with self.connect() as db:
            tweets = [dict(r) for r in db.execute("SELECT * FROM tweets ORDER BY created,id")]
            groups = {}
            for row in db.execute("SELECT * FROM media ORDER BY seq"):
                groups.setdefault(row["tweet_id"], []).append(dict(row))
            for row in tweets:
                row["media"] = groups.get(row["id"], [])
            return tweets

    def update_tweet(self, tweet_id, **values):
        allowed = {"username", "display_name", "status", "error", "error_code", "auth_mode"}
        if not values or not values.keys() <= allowed:
            raise ValueError("Invalid tweet fields")
        with self.connect() as db:
            if "status" in values:
                previous = db.execute("SELECT status FROM tweets WHERE id=?", (tweet_id,)).fetchone()
                if previous and previous[0] == 'deleting':
                    return
                if previous and previous[0] != values["status"]:
                    values["finished_at"] = time.time() if values["status"] in ("done", "failed") else None
            db.execute("UPDATE tweets SET " + ",".join(k + "=?" for k in values) + " WHERE id=?", (*values.values(), tweet_id))

    def update_media(self, tweet_id, media_id, **values):
        allowed = {"status", "error", "downloaded", "total", "sha256", "speed", "filename", "ext", "url", "source_sha256"}
        if not values or not values.keys() <= allowed:
            raise ValueError("Invalid media fields")
        with self.connect() as db:
            db.execute("UPDATE media SET " + ",".join(k + "=?" for k in values) + " WHERE tweet_id=? AND media_id=?", (*values.values(), tweet_id, media_id))

    def save_tweet(self, tweet: Tweet):
        with self.connect() as db:
            saved = db.execute('SELECT download_dir FROM tweets WHERE id=?', (tweet.tweet_id,)).fetchone()
            if not saved:
                return
            destination = Path(saved[0]) if saved[0] else self.default_download
            db.execute("UPDATE tweets SET username=?,display_name=?,warning=? WHERE id=?", (tweet.username, tweet.display_name, tweet.warning, tweet.tweet_id))
            for media in tweet.media:
                previous = db.execute("SELECT * FROM media WHERE tweet_id=? AND media_id=?", (tweet.tweet_id, media.media_id)).fetchone()
                if previous:
                    db.execute("UPDATE media SET url=? WHERE tweet_id=? AND media_id=?", (media.url, tweet.tweet_id, media.media_id))
                    continue
                while True:
                    unique = uuid.uuid4().hex
                    name = filename(tweet.username, tweet.display_name, media.index, unique, media.extension)
                    if (destination / name).exists() or db.execute("SELECT 1 FROM media WHERE unique_id=? OR filename=?", (unique, name)).fetchone():
                        continue
                    break
                db.execute("""INSERT INTO media(tweet_id,media_id,seq,kind,url,ext,unique_id,filename,status,error,download_dir)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (tweet.tweet_id, media.media_id, media.index, media.kind, media.url, media.extension, unique, name, "failed" if media.error else "queued", media.error, saved[0]))

    def setting(self, key, default=""):
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row[0] if row else default

    def set_setting(self, key, value):
        with self.connect() as db:
            db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
