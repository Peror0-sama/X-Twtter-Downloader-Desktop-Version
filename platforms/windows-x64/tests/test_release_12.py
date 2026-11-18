from contextlib import closing
import sqlite3
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image

from xdownloader.core import DownloadError, Media, Tweet, normalize_url
from xdownloader.conversion import convert_mp4_to_gif, ffmpeg_path
from xdownloader.engine import Engine
from xdownloader.storage import Store, HISTORY_SECONDS
from tests.test_app import TARGET, folder, FakeSession

SECOND = normalize_url('https://x.com/alice/status/987654321')


class ExitCleanupTests(unittest.TestCase):
    def test_cleanup_preserves_completed_and_unrelated_files_and_restores_queue(self):
        store = Store(folder())
        store.add_urls([TARGET, SECOND], gif_mode='gif')
        store.save_tweet(Tweet(TARGET.tweet_id, 'alice', 'Alice', [
            Media('one', 1, 'animated_gif', 'https://video.twimg.com/one.mp4', 'mp4'),
            Media('two', 2, 'photo', 'https://pbs.twimg.com/two.png', 'png')]))
        rows = store.media(TARGET.tweet_id)
        incomplete, complete = [store.media_path(m) for m in rows]
        partials = [incomplete.with_name(incomplete.stem + s) for s in ('.source.part', '.gif.part')]
        for path in partials: path.write_bytes(b'partial')
        complete.write_bytes(b'completed file')
        unrelated = store.download / 'user.source.part'; unrelated.write_bytes(b'user data')
        store.update_media(TARGET.tweet_id, 'one', status='converting', source_sha256='old', downloaded=23)
        store.update_media(TARGET.tweet_id, 'two', status='done')
        store.update_tweet(TARGET.tweet_id, status='downloading')
        store.update_tweet(SECOND.tweet_id, status='failed', error='keep failure')
        engine = Engine(store)
        with self.assertRaises(RuntimeError): engine.cleanup_interrupted([TARGET.tweet_id])
        engine.shutdown()
        self.assertEqual(engine.cleanup_interrupted([TARGET.tweet_id]), [])
        self.assertTrue(all(not p.exists() for p in partials))
        self.assertEqual(complete.read_bytes(), b'completed file')
        self.assertEqual(unrelated.read_bytes(), b'user data')
        reopened = Store(store.data.parent)
        self.assertEqual(reopened.tweet(TARGET.tweet_id)['status'], 'queued')
        self.assertEqual(reopened.tweet(SECOND.tweet_id)['status'], 'failed')
        self.assertEqual(reopened.media(TARGET.tweet_id)[0]['source_sha256'], '')
        self.assertEqual(reopened.media(TARGET.tweet_id)[1]['status'], 'done')
        self.assertEqual(reopened.batch_mode(reopened.tweet(TARGET.tweet_id)['batch_id']), 'gif')

    def test_cleanup_reports_locked_file_without_losing_queue(self):
        store = Store(folder()); store.add_urls([TARGET])
        store.save_tweet(Tweet(TARGET.tweet_id, 'alice', 'Alice', [Media('one', 1, 'video', 'https://video.twimg.com/one.mp4', 'mp4')]))
        engine = Engine(store); engine.shutdown()
        with patch('pathlib.Path.unlink', side_effect=PermissionError('locked')):
            self.assertEqual(len(engine.cleanup_interrupted([TARGET.tweet_id])), 3)
        self.assertEqual(store.tweet(TARGET.tweet_id)['status'], 'queued')


class HistoryTests(unittest.TestCase):
    def test_expiration_uses_finish_time_keeps_active_and_downloads(self):
        store = Store(folder())
        store.add_urls([TARGET, SECOND])
        store.update_tweet(TARGET.tweet_id, status='done')
        ended = store.tweet(TARGET.tweet_id)['finished_at']
        saved = store.download / 'existing.mp4'
        saved.write_bytes(b'user file')
        self.assertEqual(store.clear_history(True, now=ended + HISTORY_SECONDS - 1), 0)
        self.assertEqual(store.clear_history(True, now=ended + HISTORY_SECONDS), 1)
        self.assertIsNotNone(store.tweet(SECOND.tweet_id))
        self.assertEqual(saved.read_bytes(), b'user file')
        self.assertEqual(store.add_urls([TARGET, SECOND]), ([TARGET.tweet_id], 1))

    def test_manual_clear_releases_ids_but_retains_pending_and_files(self):
        store = Store(folder())
        store.add_urls([TARGET, SECOND])
        store.save_tweet(Tweet(TARGET.tweet_id, 'alice', 'Alice', [Media('m', 1, 'photo', 'https://pbs.twimg.com/a.png', 'png')]))
        store.update_tweet(TARGET.tweet_id, status='failed')
        path = store.download / store.media(TARGET.tweet_id)[0]['filename']
        path.write_bytes(b'existing')
        engine = Engine(store)
        try:
            self.assertEqual(engine.clear_history(), 1)
            self.assertEqual(store.media(TARGET.tweet_id), [])
            self.assertTrue(path.exists())
            self.assertIsNotNone(store.tweet(SECOND.tweet_id))
            self.assertEqual(store.add_urls([TARGET, TARGET]), ([TARGET.tweet_id], 1))
        finally:
            engine.shutdown()

    def test_engine_does_not_clear_finishing_worker(self):
        store = Store(folder())
        store.add_urls([TARGET])
        store.update_tweet(TARGET.tweet_id, status='done')
        engine = Engine(store)
        engine.pending[TARGET.tweet_id] = object()
        try:
            self.assertEqual(engine.clear_history(), 0)
        finally:
            engine.pending.clear()
            engine.shutdown()

    def test_expired_history_pruned_before_import_and_on_restart(self):
        store = Store(folder())
        store.add_urls([TARGET, SECOND])
        store.update_tweet(TARGET.tweet_id, status='done')
        store.update_tweet(SECOND.tweet_id, status='failed')
        with store.connect() as db:
            db.execute('UPDATE tweets SET finished_at=?', (time.time() - HISTORY_SECONDS - 1,))
        self.assertEqual(store.add_urls([TARGET]), ([TARGET.tweet_id], 0))
        self.assertIsNone(Store(store.base).tweet(SECOND.tweet_id))

    def test_retry_resets_retention_clock(self):
        store = Store(folder())
        store.add_urls([TARGET])
        with patch('xdownloader.storage.time.time', return_value=100):
            store.update_tweet(TARGET.tweet_id, status='failed')
        store.update_tweet(TARGET.tweet_id, status='queued')
        self.assertIsNone(store.tweet(TARGET.tweet_id)['finished_at'])
        with patch('xdownloader.storage.time.time', return_value=900):
            store.update_tweet(TARGET.tweet_id, status='done')
        self.assertEqual(store.tweet(TARGET.tweet_id)['finished_at'], 900)

    def test_old_schema_migrates_without_losing_history(self):
        base = folder()
        (base / 'data').mkdir()
        with closing(sqlite3.connect(base / 'data/queue.sqlite3')) as db:
            db.executescript("""CREATE TABLE tweets(id TEXT PRIMARY KEY,url TEXT NOT NULL,username TEXT NOT NULL,
                display_name TEXT DEFAULT '',status TEXT DEFAULT 'queued',error TEXT DEFAULT '',error_code TEXT DEFAULT '',
                auth_mode TEXT DEFAULT 'anonymous',created TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE media(tweet_id TEXT,media_id TEXT,seq INTEGER,kind TEXT,url TEXT,ext TEXT,unique_id TEXT UNIQUE,
                filename TEXT UNIQUE,status TEXT,error TEXT,downloaded INTEGER,total INTEGER,sha256 TEXT,speed REAL,
                PRIMARY KEY(tweet_id,media_id));""")
            db.execute("INSERT INTO tweets(id,url,username,status) VALUES(?,?,?,'done')", (TARGET.tweet_id, TARGET.url, 'alice'))
            db.commit()
        store = Store(base)
        row = store.tweet(TARGET.tweet_id)
        self.assertEqual(row['status'], 'done')
        self.assertEqual(store.batch_mode(row['batch_id']), 'mp4')
        self.assertIsNotNone(row['finished_at'])
        self.assertEqual(store.add_urls([TARGET]), ([], 1))

    def test_concurrent_duplicate_import_is_single_tweet(self):
        from concurrent.futures import ThreadPoolExecutor
        store = Store(folder())
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: store.add_urls([TARGET]), range(8)))
        self.assertEqual(sum(len(a) for a, _ in results), 1)
        self.assertEqual(sum(d for _, d in results), 7)


class GifBatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = folder() / 'sample.mp4'
        subprocess.run([str(ffmpeg_path()), '-v', 'error', '-nostdin', '-f', 'lavfi', '-i',
                        'testsrc=size=32x32:rate=8:duration=1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(cls.source)],
                       check=True, timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        cls.video = cls.source.read_bytes()

    def setUp(self):
        self.store = Store(folder())
        self.engine = Engine(self.store)

    def tearDown(self):
        self.engine.shutdown()

    def parsed(self, target=TARGET, kind='animated_gif', ext='mp4'):
        return Tweet(target.tweet_id, 'alice', 'Alice', [Media('m', 1, kind, 'https://video.twimg.com/a.mp4', ext)])

    def run_target(self, target=TARGET, kind='animated_gif'):
        with patch('xdownloader.engine.resolve', return_value=self.parsed(target, kind)), patch('xdownloader.engine.make_session', return_value=FakeSession(self.video)):
            self.engine.run_tweet(target.tweet_id)

    def test_batch_waits_without_transfer_and_choice_survives_restart(self):
        self.store.add_urls([TARGET, SECOND])
        with patch('xdownloader.engine.make_session') as transfer, patch('xdownloader.engine.resolve', side_effect=[self.parsed(), self.parsed(SECOND)]):
            self.engine.run_tweet(TARGET.tweet_id)
            self.engine.run_tweet(SECOND.tweet_id)
            transfer.assert_not_called()
        batches = self.store.waiting_batches()
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]['tweet_count'], 2)
        self.store.choose_batch_format(batches[0]['id'], 'gif')
        self.store.choose_batch_format(batches[0]['id'], 'mp4')
        reopened = Store(self.store.base)
        self.assertEqual(reopened.batch_mode(batches[0]['id']), 'gif')
        self.assertEqual(reopened.tweet(TARGET.tweet_id)['status'], 'queued')
        self.assertEqual(reopened.waiting_batches(), [])

    def test_same_batch_choice_applies_to_later_tweet_other_batch_independent(self):
        self.store.add_urls([TARGET, SECOND])
        batch = self.store.tweet(TARGET.tweet_id)['batch_id']
        self.store.choose_batch_format(batch, 'gif')
        self.run_target()
        self.run_target(SECOND)
        for target in (TARGET, SECOND):
            media = self.store.media(target.tweet_id)[0]
            self.assertEqual((media['status'], media['ext']), ('done', 'gif'))
            with Image.open(self.store.download / media['filename']) as image:
                self.assertGreater(image.n_frames, 1)
                self.assertEqual(image.size, (32, 32))
        other = normalize_url('https://x.com/alice/status/55555555')
        self.store.add_urls([other])
        self.run_target(other)
        self.assertEqual(self.store.tweet(other.tweet_id)['status'], 'waiting_choice')

    def test_plain_video_never_prompts_or_converts(self):
        self.store.add_urls([TARGET], gif_mode='gif')
        with patch('xdownloader.engine.convert_mp4_to_gif') as converter:
            self.run_target(kind='video')
        converter.assert_not_called()
        self.assertEqual(self.store.waiting_batches(), [])
        self.assertEqual(self.store.media(TARGET.tweet_id)[0]['ext'], 'mp4')

    def test_individual_formats_mix_outputs_persist_and_stay_locked(self):
        self.store.add_urls([TARGET, SECOND], gif_mode='individual')
        parsed = Tweet(TARGET.tweet_id, 'alice', 'Alice', [
            Media('one',1,'animated_gif','https://video.twimg.com/a.mp4','mp4'),
            Media('two',2,'animated_gif','https://video.twimg.com/b.mp4','mp4'),
            Media('video',3,'video','https://video.twimg.com/c.mp4','mp4')])
        with patch('xdownloader.engine.resolve',return_value=parsed),patch('xdownloader.engine.make_session') as transfer:
            self.engine.run_tweet(TARGET.tweet_id);transfer.assert_not_called()
        pending = self.store.waiting_individual()
        self.assertEqual([m['media_id'] for m in pending[0]['media']],['one','two'])
        with self.assertRaises(ValueError):self.store.choose_media_formats(TARGET.tweet_id,{'one':'mp4'})
        self.assertTrue(all(m['gif_mode'] is None for m in self.store.media(TARGET.tweet_id)))
        self.assertTrue(self.store.choose_media_formats(TARGET.tweet_id,{'one':'mp4','two':'gif'}))
        self.assertFalse(self.store.choose_media_formats(TARGET.tweet_id,{'one':'gif','two':'mp4'}))
        reopened=Store(self.store.base)
        self.assertEqual([m['gif_mode'] for m in reopened.media(TARGET.tweet_id)],['mp4','gif',None])
        with patch('xdownloader.engine.resolve',return_value=parsed),patch('xdownloader.engine.make_session',side_effect=lambda *a:FakeSession(self.video)):
            self.engine.run_tweet(TARGET.tweet_id)
        rows=self.store.media(TARGET.tweet_id)
        self.assertEqual([m['ext'] for m in rows],['mp4','gif','mp4'])
        self.assertEqual([m['status'] for m in rows],['done']*3)
        self.assertEqual(self.store.media_path(rows[0]).read_bytes(),self.video)
        with Image.open(self.store.media_path(rows[1])) as image:self.assertGreater(image.n_frames,1)
        self.run_target(SECOND)
        self.assertEqual(self.store.tweet(SECOND.tweet_id)['status'],'waiting_choice')
        self.assertEqual(self.store.waiting_individual()[0]['id'],SECOND.tweet_id)

    def test_individual_column_migration_preserves_existing_batch(self):
        self.store.add_urls([TARGET],gif_mode='gif')
        self.store.save_tweet(self.parsed())
        before=self.store.media(TARGET.tweet_id)[0]['filename']
        with self.store.connect() as db:db.execute('ALTER TABLE media DROP COLUMN gif_mode')
        reopened=Store(self.store.base)
        self.assertEqual(reopened.media(TARGET.tweet_id)[0]['filename'],before)
        self.assertIsNone(reopened.media(TARGET.tweet_id)[0]['gif_mode'])
        self.assertEqual(reopened.batch_mode(reopened.tweet(TARGET.tweet_id)['batch_id']),'gif')

    def test_direct_gif_is_saved_without_prompt_or_conversion(self):
        import io
        data = io.BytesIO()
        Image.new('RGB', (8, 8), 'red').save(data, format='GIF')
        self.store.add_urls([TARGET])
        with patch('xdownloader.engine.resolve', return_value=self.parsed(ext='gif')), patch('xdownloader.engine.make_session', return_value=FakeSession(data.getvalue())), patch('xdownloader.engine.convert_mp4_to_gif') as convert:
            self.engine.run_tweet(TARGET.tweet_id)
        convert.assert_not_called()
        self.assertEqual(self.store.waiting_batches(), [])
        media = self.store.media(TARGET.tweet_id)[0]
        self.assertEqual((self.store.download / media['filename']).read_bytes(), data.getvalue())
        self.assertEqual(media['status'], 'done')

    def test_keep_mp4_and_duplicate_do_not_download_again(self):
        self.store.add_urls([TARGET], gif_mode='mp4')
        self.run_target()
        media = self.store.media(TARGET.tweet_id)[0]
        path = self.store.download / media['filename']
        before = path.stat().st_mtime_ns
        self.assertEqual(path.read_bytes(), self.video)
        self.assertEqual(self.store.add_urls([normalize_url(TARGET.url + '/mediaViewer?currentTweet=1')]), ([], 1))
        with patch('xdownloader.engine.resolve') as resolve:
            self.engine.run_tweet(TARGET.tweet_id)
            resolve.assert_not_called()
        self.assertEqual(path.stat().st_mtime_ns, before)

    def test_clear_then_redownload_new_filename_keeps_existing(self):
        self.store.add_urls([TARGET], gif_mode='mp4')
        self.run_target()
        first = self.store.media(TARGET.tweet_id)[0]['filename']
        self.engine.clear_history()
        self.store.add_urls([TARGET], gif_mode='gif')
        self.run_target()
        second = self.store.media(TARGET.tweet_id)[0]['filename']
        self.assertNotEqual(first, second)
        self.assertTrue((self.store.download / first).exists())
        self.assertTrue((self.store.download / second).exists())

    def test_failed_conversion_retries_without_downloading_source_again(self):
        self.store.add_urls([TARGET], gif_mode='gif')
        with patch('xdownloader.engine.convert_mp4_to_gif', side_effect=DownloadError('conversion_failed', 'test failure')):
            self.run_target()
        self.assertEqual(self.store.tweet(TARGET.tweet_id)['status'], 'failed')
        with patch('xdownloader.engine.resolve', return_value=self.parsed()), patch('xdownloader.engine.make_session') as session:
            self.engine.run_tweet(TARGET.tweet_id)
            session.assert_not_called()
        media = self.store.media(TARGET.tweet_id)[0]
        self.assertEqual((media['status'], media['ext']), ('done', 'gif'))
        self.assertEqual(list(self.store.download.glob('*.part')), [])

    def test_conversion_stop_and_invalid_source_are_not_success(self):
        target = self.store.download / 'cancel.gif.part'
        stop = threading.Event()
        stop.set()
        with self.assertRaises(DownloadError) as cm:
            convert_mp4_to_gif(self.source, target, stop)
        self.assertEqual(cm.exception.code, 'interrupted')
        bad = self.store.download / 'bad.mp4'
        bad.write_bytes(b'not media')
        with self.assertRaises(DownloadError):
            convert_mp4_to_gif(bad, target, threading.Event())
        self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
