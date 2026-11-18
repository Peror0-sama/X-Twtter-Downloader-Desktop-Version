import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from xdownloader import account
from xdownloader.core import DownloadError, Media, Tweet, normalize_url, parse_page
from xdownloader.engine import Engine
from xdownloader.hls import get_bytes
from xdownloader.storage import Store

BROADCAST = normalize_url('https://x.com/i/broadcasts/1djGXWRYzzdKZ')
POST = normalize_url('https://x.com/alice/status/123456789')


class BroadcastAccountTests(unittest.TestCase):
    def test_public_broadcast_resolves_without_sending_login_cookies(self):
        expected = Tweet(BROADCAST.tweet_id, 'setsu_mimi', 'せつねこ@Xくん怒らないでください', [])
        with patch.object(account, 'resolve', return_value=expected) as public, patch.object(account, '_authenticated_page') as private:
            result = account.resolve_authenticated(BROADCAST, 'system', '', {'auth_token': 'test-only'})
        self.assertIs(result, expected)
        public.assert_called_once_with(BROADCAST, 'system', '')
        private.assert_not_called()

    def test_restricted_broadcast_falls_back_to_account(self):
        cookies = {'auth_token': 'test-only'}
        with patch.object(account, 'resolve', side_effect=DownloadError('access_denied', 'login needed')), patch.object(account, '_authenticated_page', return_value='account-result') as private:
            self.assertEqual(account.resolve_authenticated(BROADCAST, 'system', '', cookies), 'account-result')
        private.assert_called_once_with(BROADCAST, 'system', '', cookies, None)

    def test_network_failure_does_not_trigger_another_attempt(self):
        with patch.object(account, 'resolve', side_effect=DownloadError('timeout', 'timeout', True)) as public, patch.object(account, '_authenticated_page') as private:
            with self.assertRaises(DownloadError):
                account.resolve_authenticated(BROADCAST, 'system', '', {'auth_token': 'test-only'})
        public.assert_called_once()
        private.assert_not_called()

    def test_cancelled_broadcast_does_not_start_public_request(self):
        stop = threading.Event(); stop.set()
        with patch.object(account, 'resolve') as public, self.assertRaises(DownloadError):
            account.resolve_authenticated(BROADCAST, 'system', '', {'auth_token': 'test-only'}, stop)
        public.assert_not_called()


class ManualRetryTests(unittest.TestCase):
    def test_parse_failure_runs_once_and_manual_retry_still_works(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory)); store.add_urls([POST])
            engine = Engine(store)
            try:
                with patch('xdownloader.engine.resolve', side_effect=DownloadError('timeout', 'timeout', True)) as resolve:
                    engine.run_tweet(POST.tweet_id)
                    self.assertEqual(store.tweet(POST.tweet_id)['status'], 'failed')
                    resolve.assert_called_once()
                    self.assertTrue(engine.retry(POST.tweet_id, 'anonymous'))
                    deadline = time.monotonic() + 3
                    while engine.busy() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertFalse(engine.busy())
                    self.assertEqual(resolve.call_count, 2)
            finally:
                engine.shutdown()

    def test_media_failure_runs_once(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory)); store.add_urls([POST])
            store.save_tweet(Tweet(POST.tweet_id, 'alice', 'Alice', [Media('1', 1, 'video', 'https://video.twimg.com/a.mp4', 'mp4')]))
            engine = Engine(store)
            try:
                with patch.object(engine, '_download', side_effect=DownloadError('truncated', 'truncated', True)) as download:
                    engine.run_media(POST.tweet_id, store.media(POST.tweet_id)[0], 'direct', '')
                download.assert_called_once()
                self.assertEqual(store.media(POST.tweet_id)[0]['status'], 'failed')
            finally:
                engine.shutdown()

    def test_hls_segment_failure_runs_once(self):
        session = MagicMock(); session.__enter__.return_value = session
        session.get.side_effect = requests.Timeout('timeout')
        with patch('xdownloader.hls.make_session', return_value=session), self.assertRaises(requests.Timeout):
            get_bytes('https://video.twimg.com/a.ts', 'direct', '', threading.Event(), 1024)
        session.get.assert_called_once()


class LoginCloseTests(unittest.TestCase):
    def test_close_keeps_ui_responsive_and_cleans_browser_once(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide2.QtCore import QTimer, QEventLoop
        from PySide2.QtTest import QTest
        from PySide2.QtWidgets import QApplication
        from xdownloader.login import LoginDialog
        app = QApplication.instance() or QApplication([])
        started, release, completed = threading.Event(), threading.Event(), threading.Event()
        process = MagicMock()
        def slow_close():
            started.set()
            release.wait(3)
            completed.set()
        process.close.side_effect = slow_close
        saved = MagicMock(); saved.cookies.return_value = {}
        with patch.object(LoginDialog, 'start_browser'):
            dialog = LoginDialog(saved, 'system', '')
            dialog.show(); app.processEvents()
            dialog.process = process
            try:
                before = time.monotonic()
                dialog.reject()
                self.assertLess(time.monotonic() - before, 0.5)
                self.assertTrue(started.wait(1))
                pulse = []
                QTimer.singleShot(0, lambda: pulse.append(True))
                loop = QEventLoop(); QTimer.singleShot(30, loop.quit); loop.exec_()
                self.assertEqual(pulse, [True])
                self.assertFalse(completed.is_set())
                dialog.shutdown()
                process.close.assert_called_once()
                self.assertIsNone(dialog.process)
                self.assertFalse(dialog.timer.isActive())
            finally:
                release.set(); self.assertTrue(completed.wait(2))
                dialog.deleteLater(); app.processEvents()


class AuthorIdentityTests(unittest.TestCase):
    def test_missing_tweet_typename_keeps_target_author_and_unicode_name(self):
        from tests.test_app import page, entity
        target = normalize_url('https://x.com/Vtuber_ti/status/2122029069646839831')
        user = {'__typename':'User','rest_id':'123','core':{'screen_name':'Vtuber_ti','name':'佐藤ちゆり💞💉'}}
        post = {'rest_id':target.tweet_id,'core':{'user_results':{'result':user}},'legacy':{'id_str':target.tweet_id,'user_id_str':'123','extended_entities':{'media':[entity(kind='video')]}}}
        unrelated = {'__typename':'Tweet','rest_id':'999999999','core':{'user_results':{'result':{'core':{'screen_name':'wrong','name':'Wrong author'}}}}}
        result = parse_page(page({'target':post,'other':unrelated}), target, True)
        self.assertEqual((result.username,result.display_name),('Vtuber_ti','佐藤ちゆり💞💉'))
        self.assertEqual(result.warning,'')
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory));store.add_urls([target]);store.save_tweet(result)
            self.assertIn('佐藤ちゆり',store.media(target.tweet_id)[0]['filename'])

    def test_flat_legacy_resolves_only_the_linked_user_id(self):
        from tests.test_app import page, entity
        post = {'id_str':POST.tweet_id,'user_id_str':'123','extended_entities':{'media':[entity()]}}
        users = [{'__typename':'User','rest_id':'999','core':{'screen_name':'wrong','name':'Wrong'}}, {'__typename':'User','rest_id':'123','core':{'screen_name':'alice','name':'Alice'}}]
        result=parse_page(page({'post':post,'users':users}), POST)
        self.assertEqual((result.username,result.display_name),('alice','Alice'))
        self.assertFalse(result.warning)


class HlsConnectionTests(unittest.TestCase):
    def test_segment_connection_can_be_reused_without_closing_or_retry(self):
        from tests.test_app import FakeResponse
        session=MagicMock();session.get.side_effect=[FakeResponse(b'first'),FakeResponse(b'second')]
        with patch('xdownloader.hls.make_session') as factory:
            for suffix in ('a','b'):
                get_bytes('https://video.twimg.com/'+suffix+'.ts','direct','',threading.Event(),1024,session=session)
        factory.assert_not_called()
        session.close.assert_not_called()
        self.assertEqual(session.get.call_count,2)
