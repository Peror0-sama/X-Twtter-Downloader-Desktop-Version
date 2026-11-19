import io
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from xdownloader.core import Media, Tweet, DownloadError, normalize_url
from xdownloader.engine import Engine, _StopSignal
from xdownloader.storage import Store

TARGET = normalize_url('https://x.com/example/status/123456789')


def wait_for(test, predicate, seconds=20):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.01)
    test.fail('Timed out waiting for state')


class PauseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.store.add_urls([TARGET], gif_mode='mp4')
        self.engine = Engine(self.store)

    def tearDown(self):
        self.engine.shutdown()
        self.tmp.cleanup()

    def test_pause_stops_conversion_process_and_resume_restarts_local_work(self):
        from xdownloader.conversion import _run
        pause = threading.Event()
        stop = _StopSignal(threading.Event(), pause=pause)
        processes = []
        class Process:
            returncode = None
            def __init__(self): processes.append(self)
            def poll(self): return self.returncode
            def terminate(self): self.returncode = -1
            def wait(self, **kwargs): return self.returncode
        errors = []
        def run():
            try: _run(['local-input'], stop)
            except Exception as exc: errors.append(exc)
        with patch('xdownloader.conversion.subprocess.Popen', side_effect=lambda *a, **k: Process()), patch('xdownloader.conversion.ffmpeg_path', return_value=Path('ffmpeg')):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                wait_for(self, lambda: len(processes) == 1)
                pause.set()
                wait_for(self, lambda: processes[0].returncode == -1)
                time.sleep(.2)
                self.assertEqual(len(processes), 1)
                pause.clear()
                wait_for(self, lambda: len(processes) == 2)
                processes[1].returncode = 0
                worker.join(3)
            finally:
                pause.clear()
                stop.global_stop.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def test_queued_pause_blocks_dispatch_and_survives_restart(self):
        self.assertTrue(self.engine.pause(TARGET.tweet_id))
        self.engine.enqueue(TARGET.tweet_id)
        self.assertFalse(self.engine.busy())
        reopened = Store(Path(self.tmp.name))
        self.assertEqual(reopened.tweet(TARGET.tweet_id)['status'], 'paused')
        self.assertEqual(reopened.clear_history(), 0)

    def test_hls_pause_preserves_segment_order_and_resumes_real_merge(self):
        import subprocess
        from xdownloader.conversion import ffmpeg_path
        from xdownloader.engine import verify_file
        source = Path(self.tmp.name) / 'segments'
        source.mkdir()
        subprocess.run([str(ffmpeg_path()), '-v', 'error', '-f', 'lavfi', '-i',
                        'testsrc=size=64x64:rate=10', '-t', '4', '-c:v', 'mpeg2video',
                        '-g', '10', '-sc_threshold', '0', '-f', 'hls', '-hls_time', '1',
                        '-hls_list_size', '0', '-hls_segment_filename', str(source/'%02d.ts'),
                        str(source/'index.m3u8')], check=True, timeout=30,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        urls = ['https://video.twimg.com/' + p.name for p in sorted(source.glob('*.ts'))]
        self.assertGreater(len(urls), 1)
        tweet = Tweet(TARGET.tweet_id, 'example', 'Example',
                      [Media('m', 1, 'video', 'https://video.twimg.com/master.m3u8', 'mp4')])
        seen = []
        update = self.engine.set_transfer_progress
        def record(tid, mid, **values):
            update(tid, mid, **values)
            if values.get('_phase') == 'downloading':
                seen.append(values['_segments_done'])
                if values['_segments_done'] == 1:
                    self.engine.pause(tid)
        def fetch(url, *args, **kwargs):
            return (source/url.rsplit('/', 1)[-1]).read_bytes(), url
        with patch('xdownloader.engine.resolve', return_value=tweet), \
             patch('xdownloader.hls.media_playlist', return_value=(urls, 4)), \
             patch('xdownloader.hls.get_bytes', side_effect=fetch), \
             patch.object(self.engine, 'set_transfer_progress', side_effect=record):
            self.engine.enqueue(TARGET.tweet_id)
            wait_for(self, lambda: self.store.tweet(TARGET.tweet_id)['status'] == 'paused')
            time.sleep(.3)
            self.assertEqual(seen, [0, 1])
            self.engine.resume(TARGET.tweet_id)
            wait_for(self, lambda: not self.engine.busy())
        self.assertEqual(self.store.tweet(TARGET.tweet_id)['status'], 'done')
        self.assertEqual(seen, list(range(len(urls)+1)))
        self.assertEqual(verify_file(self.store.media_path(self.store.media(TARGET.tweet_id)[0]))[0], 'mp4')

    def test_pause_transfer_preserves_partial_and_resumes_same_request(self):
        image = Image.effect_noise((700, 700), 100).convert('RGB')
        data = io.BytesIO(); image.save(data, format='PNG'); payload = data.getvalue()
        requests = []
        class Response:
            status_code = 200
            headers = {'Content-Length': str(len(payload)), 'Content-Type': 'image/png'}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def iter_content(self, size):
                for pos in range(0, len(payload), 8192):
                    time.sleep(0.02)
                    yield payload[pos:pos + 8192]
        class Session:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def get(self, *args, **kwargs):
                requests.append(args[0]);return Response()
        media = Media('one', 1, 'photo', 'https://pbs.twimg.com/media/valid.png', 'png')
        tweet = Tweet(TARGET.tweet_id, TARGET.username, '测试', [media])
        with patch('xdownloader.engine.resolve', return_value=tweet), patch('xdownloader.engine.make_session', return_value=Session()):
            self.engine.enqueue(TARGET.tweet_id)
            wait_for(self, lambda: bool(list(self.store.download.glob('*.part'))) and next(self.store.download.glob('*.part')).stat().st_size > 16384)
            self.assertTrue(self.engine.pause(TARGET.tweet_id))
            time.sleep(0.12)
            part = next(self.store.download.glob('*.part')); size = part.stat().st_size
            time.sleep(0.25)
            self.assertEqual(part.stat().st_size, size)
            self.assertEqual(self.store.tweet(TARGET.tweet_id)['status'], 'paused')
            self.assertTrue(self.engine.resume(TARGET.tweet_id))
            wait_for(self, lambda: not self.engine.busy())
        self.assertEqual(self.store.tweet(TARGET.tweet_id)['status'], 'done')
        self.assertEqual(len(requests), 1)
        saved = self.store.media_path(self.store.media(TARGET.tweet_id)[0])
        self.assertEqual(saved.read_bytes(), payload)

    def test_cancel_wakes_paused_waiter(self):
        global_stop, cancel, pause = threading.Event(), threading.Event(), threading.Event()
        pause.set();signal = _StopSignal(global_stop, cancel, pause)
        result = [];thread = threading.Thread(target=lambda: result.append(signal.is_set()))
        thread.start();time.sleep(.1)
        self.assertTrue(thread.is_alive())
        cancel.set();thread.join(1)
        self.assertEqual(result, [True])

    def test_shutdown_does_not_hang_paused_resolver(self):
        entered = threading.Event()
        def resolve(*args):
            entered.set();time.sleep(.1)
            self.engine.check_stop(TARGET.tweet_id)
            return Tweet(TARGET.tweet_id, TARGET.username, '测试', [])
        with patch('xdownloader.engine.resolve', side_effect=resolve):
            self.engine.enqueue(TARGET.tweet_id);self.assertTrue(entered.wait(1))
            self.engine.pause(TARGET.tweet_id)
            self.engine.shutdown()
        self.assertFalse(self.engine.busy())
        self.engine.cleanup_interrupted([TARGET.tweet_id])
        self.assertEqual(self.store.tweet(TARGET.tweet_id)['status'], 'paused')

    def test_delete_paused_plan_wakes_and_removes_it(self):
        entered = threading.Event()
        def resolve(*args):
            entered.set();time.sleep(.1)
            self.engine.check_stop(TARGET.tweet_id)
            return Tweet(TARGET.tweet_id, TARGET.username, '测试', [])
        with patch('xdownloader.engine.resolve', side_effect=resolve):
            self.engine.enqueue(TARGET.tweet_id);self.assertTrue(entered.wait(1))
            self.engine.pause(TARGET.tweet_id)
            self.assertEqual(self.engine.delete_plans([TARGET.tweet_id]), 1)
            wait_for(self, lambda: not self.engine.busy())
        self.assertIsNone(self.store.tweet(TARGET.tweet_id))

    def test_failed_and_done_are_not_paused(self):
        for status in ('failed', 'done'):
            self.store.update_tweet(TARGET.tweet_id, status=status)
            self.assertFalse(self.engine.pause(TARGET.tweet_id))

    def test_resolver_deadline_excludes_pause(self):
        pause = threading.Event();pause.set();signal = _StopSignal(threading.Event(), pause=pause)
        timer=threading.Timer(.2,pause.clear);timer.start()
        self.assertFalse(signal.is_set())
        self.assertGreaterEqual(signal.paused_seconds,.15)

    def test_paused_worker_result_not_overwritten_before_resume(self):
        self.engine.pause(TARGET.tweet_id)
        self.engine.update_state(TARGET.tweet_id, status='failed',error='模拟网络失败',error_code='network')
        self.assertEqual(self.store.tweet(TARGET.tweet_id)['status'],'paused')
        self.engine.resume(TARGET.tweet_id)
        self.assertEqual(self.store.tweet(TARGET.tweet_id)['status'],'failed')


class FailureReasonTests(unittest.TestCase):
    def test_tombstone_text_confirms_deletion(self):
        from xdownloader.account import parse_account_payload
        payload={'data':{'tweetResult':{'result':{'__typename':'TweetTombstone','tombstone':{'text':{'text':'This Post was deleted by the Post author.'}}}}}}
        with self.assertRaises(DownloadError) as error:parse_account_payload(payload,TARGET)
        self.assertEqual(error.exception.code,'deleted')

    def test_missing_result_does_not_claim_deletion(self):
        from xdownloader.account import parse_account_payload
        with self.assertRaises(DownloadError) as error:parse_account_payload({'data':{'tweetResult':{'result':None}}},TARGET)
        self.assertEqual(error.exception.code,'unavailable')
        self.assertIn('原因未确认',str(error.exception))

    def test_message_word_does_not_match_age_restriction(self):
        from xdownloader.core import unavailable_error
        self.assertEqual(unavailable_error({'message':'Unknown'}).code,'unavailable')

    def test_explicit_api_error_permissions(self):
        from xdownloader.account import parse_account_payload
        for code,expected in [(179,'private'),(63,'suspended'),(89,'login_expired'),(144,'not_found')]:
            with self.subTest(code=code),self.assertRaises(DownloadError) as error:
                parse_account_payload({'errors':[{'code':code}]},TARGET)
            self.assertEqual(error.exception.code,expected)


class BrowserStatusTests(unittest.TestCase):
    def test_official_page_status_after_empty_payload(self):
        import json, queue
        from xdownloader.browser_resolver import BrowserResolver
        events = queue.Queue()
        events.put({'type':'payload','payload':{'data':{'tweetResult':{}}}})
        for url,article in [('https://x.com/other/status/987654321',False),(TARGET.url,True)]:
            events.put({'type':'page_status','result':json.dumps(dict(url=url,has_article=article,text='此貼文已刪除'))})
        events.put({'type':'page_status','result':json.dumps(dict(url=TARGET.url,has_article=False,text='嗯…此頁面不存在。請嘗試搜尋其他內容。'))})
        class Process:
            def __init__(self,*a):self.events=events
            def send(self,command):pass
            def close(self):pass
        with patch('xdownloader.webview.BrowserProcess',Process):
            with self.assertRaises(DownloadError) as error:
                BrowserResolver().resolve(TARGET,'system','',{})
        self.assertEqual(error.exception.code,'not_found')

    def test_page_status_languages_and_unknown_are_distinct(self):
        from xdownloader.core import page_status_error
        for text,code in [('此貼文已刪除','deleted'),('此頁面不存在','not_found'),('This page doesn’t exist','not_found'),('這些貼文已受保護','private'),('帳戶已遭停權','suspended')]:
            self.assertEqual(page_status_error(text).code,code)
        self.assertIsNone(page_status_error('Loading…'))
