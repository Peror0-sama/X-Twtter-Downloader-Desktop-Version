import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from xdownloader.account import AccountStore
from xdownloader.core import DownloadError, normalize_url, parse_input, parse_page, validate_media_url
from xdownloader.hls import media_playlist
from xdownloader.storage import Store

URL = 'https://x.com/i/broadcasts/1djGXWRYzzdKZ'


class BroadcastTests(unittest.TestCase):
    def test_compressed_playlist_length_is_not_mistaken_for_truncation(self):
        from unittest.mock import MagicMock
        from tests.test_app import FakeResponse
        from xdownloader.hls import get_bytes
        response=FakeResponse(b'#EXTM3U\n'+b'# comment\n'*100)
        response.headers.update({'Content-Encoding':'gzip','Content-Length':'30'})
        session=MagicMock();session.__enter__.return_value=session;session.get.return_value=response
        with patch('xdownloader.hls.make_session',return_value=session):
            data,_=get_bytes('https://video.twimg.com/master.m3u8','direct','',threading.Event(),8192)
        self.assertEqual(data,response.data)

    def test_typo_rejected_with_correction_not_silently_enqueued(self):
        urls, errors = parse_input(URL.replace('broadcasts', 'btoadcasts'))
        self.assertEqual(urls, [])
        self.assertIn('可能为连接错误，请检查拼写', errors[0])
        self.assertIn(URL, errors[0])

    def test_broadcast_alias_deduplicates_and_uses_same_filename(self):
        target = normalize_url(URL+'?s=20')
        user = {'__typename':'User','core':{'screen_name':'alice','name':'爱丽丝'}}
        obj = {'broadcast_id':target.tweet_id,'state':'Ended','available_for_replay':True,'user_results':{'result':user}}
        html = '<meta property="og:video:url" content="https://prod-fastly-p.video.pscp.tv/test.m3u8"><script type="application/json">'+json.dumps({'data':{'broadcast_by_rest_id':obj}})+'</script>'
        tweet = parse_page(html,target)
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d));added,duplicate=store.add_urls([target,normalize_url(URL.replace('x.com','twitter.com'))])
            self.assertEqual((len(added),duplicate),(1,1))
            store.save_tweet(tweet)
            name=store.media(target.tweet_id)[0]['filename']
            self.assertRegex(name,r'^alice_爱丽丝_1_[a-f0-9]{32}\.mp4$')

    def test_unrelated_broadcast_never_downloaded(self):
        html='<script type="application/json">'+json.dumps({'broadcast_id':'other','hlsUrl':'https://video.twimg.com/a.m3u8'})+'</script>'
        with self.assertRaises(DownloadError):parse_page(html,normalize_url(URL))

    def test_hls_highest_quality_and_complete_duration(self):
        master=b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=10\nlow.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=20\nhigh.m3u8\n'
        high=b'#EXTM3U\n#EXTINF:6,\n1.ts\n#EXTINF:7,\n2.ts\n#EXT-X-ENDLIST\n'
        urls=[]
        def fetch(url,*args):
            urls.append(url);return (master if url.endswith('master.m3u8') else high),url
        with patch('xdownloader.hls.get_bytes',side_effect=fetch):
            segments,duration=media_playlist('https://video.twimg.com/master.m3u8','direct','',threading.Event())
        self.assertEqual(urls[-1],'https://video.twimg.com/high.m3u8')
        self.assertEqual(duration,13);self.assertEqual(len(segments),2)

    def test_live_encrypted_and_foreign_segments_rejected(self):
        for text in ('#EXTM3U\n#EXTINF:2,\n1.ts', '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key"\n#EXT-X-ENDLIST', '#EXTM3U\n#EXTINF:2,\nhttps://evil.example/a.ts\n#EXT-X-ENDLIST'):
            with self.subTest(text=text),patch('xdownloader.hls.get_bytes',return_value=(text.encode(),'https://video.twimg.com/a.m3u8')),self.assertRaises(DownloadError):
                media_playlist('https://video.twimg.com/a.m3u8','direct','',threading.Event())
        with self.assertRaises(DownloadError):validate_media_url('https://video.pscp.tv.evil.example/a.ts')


class LoginRetentionTests(unittest.TestCase):
    def test_restart_keeps_login_but_never_extends_30_day_deadline(self):
        with tempfile.TemporaryDirectory() as d,patch('xdownloader.account._crypt',side_effect=lambda data,_:data):
            path=Path(d)
            with patch('xdownloader.account.time.time',return_value=1000):
                account=AccountStore(path);account.save({'auth_token':'test-only'})
            with patch('xdownloader.account.time.time',return_value=1000+29*86400):
                reopened=AccountStore(path);self.assertEqual(reopened.cookies(),{'auth_token':'test-only'})
            with patch('xdownloader.account.time.time',return_value=1000+30*86400):
                self.assertEqual(reopened.cookies(),{})
                self.assertEqual(AccountStore(path).cookies(),{})

    def test_old_encrypted_account_uses_original_save_time(self):
        import os
        with tempfile.TemporaryDirectory() as d,patch('xdownloader.account._crypt',side_effect=lambda data,_:data):
            path=Path(d)/'account.bin';path.write_text('{"auth_token":"test-only"}')
            os.utime(path,(1000,1000))
            with patch('xdownloader.account.time.time',return_value=1000+31*86400):
                self.assertEqual(AccountStore(Path(d)).cookies(),{})


class InstanceTests(unittest.TestCase):
    def test_lock_is_shared_across_processes_and_released_on_exit(self):
        import subprocess
        import sys
        import uuid
        from xdownloader.instance import SingleInstance
        name='Local\\XDownloader.Test.'+uuid.uuid4().hex
        owner=SingleInstance(name)
        self.assertTrue(owner.acquire())
        code='from xdownloader.instance import SingleInstance; m=SingleInstance('+repr(name)+'); print(m.acquire()); m.close()'
        try:
            result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stdout.strip(),'False')
        finally:owner.close()
        other=SingleInstance(name)
        try:self.assertTrue(other.acquire())
        finally:other.close()


class LoginUiTests(unittest.TestCase):
    def test_address_enter_navigates_without_saving_or_closing(self):
        import os
        os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
        from unittest.mock import MagicMock
        from PySide2.QtWidgets import QApplication
        from PySide2.QtTest import QTest
        from PySide2.QtCore import Qt
        from xdownloader.login import LoginDialog
        app=QApplication.instance() or QApplication([])
        account=MagicMock();account.cookies.return_value={}
        with patch.object(LoginDialog,'start_browser'):
            dialog=LoginDialog(account,'system','')
            dialog.send=MagicMock()
            dialog.show();app.processEvents()
            QTest.mouseClick(dialog.address.cover, Qt.MouseButton.LeftButton)
            QTest.mouseClick(dialog.address, Qt.MouseButton.LeftButton)
            dialog.address.setText('https://x.com/test/status/123456789')
            QTest.keyClick(dialog.address,Qt.Key.Key_Return);app.processEvents()
            self.assertTrue(dialog.isVisible())
            account.save.assert_not_called()
            dialog.send.assert_called_with({'op':'navigate','url':'https://x.com/test/status/123456789'})
            self.assertEqual(dialog.address.edit_state, 'locked')
            dialog.close()
