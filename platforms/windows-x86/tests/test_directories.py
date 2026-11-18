import io
import sqlite3
import threading
import unittest
from unittest.mock import patch
from PIL import Image

from xdownloader.storage import Store
from xdownloader.engine import Engine
from xdownloader.core import Tweet, Media, normalize_url
from tests.test_app import folder, TARGET, FakeSession


class DirectoryTests(unittest.TestCase):
    def test_default_custom_persistence_and_return_to_default(self):
        store = Store(folder())
        self.assertEqual(store.download, store.base / 'download')
        custom = store.base / 'custom 中文 folder'
        store.set_download_directory(custom)
        self.assertEqual(Store(store.base).download, custom)
        self.assertEqual(list(custom.iterdir()), [])
        store.set_download_directory(store.default_download)
        self.assertEqual(store.setting('download_dir'), '')

    def test_switch_updates_existing_and_new_unstarted_tasks(self):
        store = Store(folder())
        store.add_urls([TARGET], gif_mode='mp4')
        custom = store.base / 'custom'
        store.set_download_directory(custom)
        second = normalize_url('https://x.com/alice/status/987654321')
        store.add_urls([second], gif_mode='mp4')
        store.set_download_directory(store.base / 'third')
        data = io.BytesIO()
        Image.new('RGB', (8, 8), 'blue').save(data, format='PNG')
        engine = Engine(store)
        try:
            for target, directory in [(TARGET,store.download),(second,store.download)]:
                tweet = Tweet(target.tweet_id,'alice','Alice',[Media('m',1,'photo','https://pbs.twimg.com/m.png','png')])
                with patch('xdownloader.engine.resolve',return_value=tweet), patch('xdownloader.engine.make_session',return_value=FakeSession(data.getvalue())):
                    engine.run_tweet(target.tweet_id)
                media = store.media(target.tweet_id)[0]
                self.assertEqual(store.media_path(media).parent,directory)
                self.assertTrue(store.media_path(media).is_file())
            self.assertEqual(len(list(store.download.iterdir())), 2)
            self.assertEqual(list(store.default_download.iterdir()), [])
            self.assertEqual(list(custom.iterdir()), [])
            self.assertEqual(store.add_urls([TARGET]), ([],1))
        finally:
            engine.shutdown()

    def test_in_flight_file_stays_and_queued_snapshot_uses_new_directory(self):
        store = Store(folder())
        store.add_urls([TARGET], gif_mode='mp4')
        store.save_tweet(Tweet(TARGET.tweet_id,'alice','Alice',[
            Media('first',1,'photo','https://pbs.twimg.com/1.png','png'),
            Media('second',2,'photo','https://pbs.twimg.com/2.png','png')]))
        first, second = store.media(TARGET.tweet_id)
        data = io.BytesIO()
        Image.new('RGB',(8,8),'blue').save(data,format='PNG')
        engine = Engine(store)
        started, release = threading.Event(), threading.Event()
        fetch = engine._fetch_part

        def delayed_fetch(tweet_id, mid, *args):
            if mid == 'first':
                started.set()
                if not release.wait(5):
                    raise RuntimeError('test download release timed out')
            return fetch(tweet_id, mid, *args)

        try:
            with patch.object(engine,'_fetch_part',side_effect=delayed_fetch), patch('xdownloader.engine.make_session',side_effect=lambda *a: FakeSession(data.getvalue())):
                future = engine.media_pool.submit(engine.run_media,TARGET.tweet_id,first,'direct','')
                try:
                    self.assertTrue(started.wait(5))
                    store.set_download_directory(store.base/'new folder')
                    engine.run_media(TARGET.tweet_id,second,'direct','')
                finally:
                    release.set()
                future.result(timeout=5)
            rows = store.media(TARGET.tweet_id)
            self.assertEqual([r['status'] for r in rows],['done','done'])
            self.assertEqual(store.media_path(rows[0]).parent,store.default_download)
            self.assertEqual(store.media_path(rows[1]).parent,store.download)
            self.assertTrue(all(store.media_path(r).is_file() for r in rows))
            self.assertEqual([r['download_dir'] for r in store.snapshot()[0]['media']],[r['download_dir'] for r in rows])
        finally:
            release.set()
            engine.shutdown()

    def test_waiting_format_and_resolving_tasks_follow_directory_change(self):
        store = Store(folder())
        store.add_urls([TARGET])
        store.update_tweet(TARGET.tweet_id,status='resolving')
        store.set_download_directory(store.base/'first choice')
        store.save_tweet(Tweet(TARGET.tweet_id,'alice','Alice',[Media('gif',1,'animated_gif','https://video.twimg.com/a.mp4','mp4')]))
        store.wait_for_format(TARGET.tweet_id)
        store.set_download_directory(store.base/'second choice')
        self.assertEqual(store.media_path(store.media(TARGET.tweet_id)[0]).parent,store.download)
        store.choose_batch_format(store.tweet(TARGET.tweet_id)['batch_id'],'gif')
        self.assertEqual(store.media_path(store.begin_media(TARGET.tweet_id,'gif')).parent,store.download)

    def test_started_retry_keeps_original_location_after_restart(self):
        store = Store(folder())
        store.add_urls([TARGET],gif_mode='mp4')
        store.save_tweet(Tweet(TARGET.tweet_id,'alice','Alice',[Media('m',1,'photo','https://pbs.twimg.com/m.png','png')]))
        media = store.begin_media(TARGET.tweet_id,'m')
        store.update_media(TARGET.tweet_id,'m',status='failed',error='interrupted')
        store = Store(store.base)
        store.set_download_directory(store.base/'new folder')
        data = io.BytesIO()
        Image.new('RGB',(8,8),'blue').save(data,format='PNG')
        engine = Engine(store)
        try:
            with patch('xdownloader.engine.make_session',return_value=FakeSession(data.getvalue())):
                engine.run_media(TARGET.tweet_id,media,'direct','')
            result = store.media(TARGET.tweet_id)[0]
            self.assertEqual(result['status'],'done')
            self.assertEqual(store.media_path(result).parent,store.default_download)
            self.assertTrue(store.media_path(result).is_file())
        finally:
            engine.shutdown()

    def test_previous_schema_preserves_completed_and_partial_paths(self):
        store = Store(folder())
        original = store.base/'old custom'
        store.set_download_directory(original)
        store.add_urls([TARGET])
        store.save_tweet(Tweet(TARGET.tweet_id,'alice','Alice',[
            Media(str(i),i,'photo',f'https://pbs.twimg.com/{i}.png','png') for i in range(1,4)]))
        rows = store.media(TARGET.tweet_id)
        store.update_media(TARGET.tweet_id,'1',status='done')
        partial = store.media_path(rows[1]).with_name(store.media_path(rows[1]).stem+'.source.part')
        partial.write_bytes(b'partial data')
        with sqlite3.connect(store.path) as db:
            db.execute('ALTER TABLE media DROP COLUMN download_started')
            db.execute('ALTER TABLE media DROP COLUMN download_dir')
        store = Store(store.base)
        store.set_download_directory(store.base/'new folder')
        rows = store.media(TARGET.tweet_id)
        self.assertEqual([store.media_path(r).parent for r in rows],[original,original,store.download])
        self.assertEqual(partial.read_bytes(),b'partial data')

    def test_unusable_directory_keeps_previous_setting(self):
        store = Store(folder())
        bad = store.base/'a file'
        bad.write_text('existing')
        with self.assertRaises(OSError):
            store.set_download_directory(bad)
        self.assertEqual(store.download, store.default_download)
        self.assertEqual(bad.read_text(), 'existing')

    def test_snapshot_paths_and_history_clear_preserve_external_files(self):
        store = Store(folder())
        custom = store.base/'outside download'
        store.set_download_directory(custom)
        store.add_urls([TARGET])
        store.save_tweet(Tweet(TARGET.tweet_id,'alice','Alice',[Media('m',1,'photo','https://pbs.twimg.com/m.png','png')]))
        path = store.media_path(store.snapshot()[0]['media'][0])
        self.assertEqual(path.parent, custom)
        path.write_bytes(b'keep')
        store.update_tweet(TARGET.tweet_id,status='done')
        self.assertEqual(store.clear_history(),1)
        self.assertEqual(path.read_bytes(),b'keep')
