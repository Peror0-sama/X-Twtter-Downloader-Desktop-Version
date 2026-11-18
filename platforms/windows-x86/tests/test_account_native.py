import threading
import unittest
from unittest.mock import MagicMock, patch

import requests

from xdownloader import account
from xdownloader.core import DownloadError, parse_page
from tests.test_app import TARGET, page, tweet_obj, FakeResponse


class NativeAccountTests(unittest.TestCase):
    def response(self, html='', status=200, location=None):
        r=FakeResponse(html.encode())
        r.status_code=status
        if location:r.headers['Location']=location
        return r

    def session(self, *responses):
        session=MagicMock()
        session.__enter__.return_value=session
        session.get.side_effect=responses
        return session

    def test_target_account_media_and_sensitive_flag_preserved(self):
        obj=tweet_obj();obj['legacy']={'possibly_sensitive':True}
        html=page(obj)+'<div>reason: "sensitiveMedia"</div>'
        session=self.session(self.response(html))
        with patch.object(account,'make_session',return_value=session):
            result=account.resolve_authenticated(TARGET,'direct','',{'auth_token':'test-token','ct0':'test-csrf','ignored':'unused'})
        self.assertEqual(len(result.media),2)
        self.assertEqual(session.cookies.set.call_count,2)
        for call in session.cookies.set.call_args_list:
            self.assertEqual(call.kwargs,{'domain':'x.com','path':'/','secure':True})
        self.assertFalse(session.get.call_args.kwargs['allow_redirects'])
        with self.assertRaises(DownloadError) as error:parse_page(html,TARGET)
        self.assertEqual(error.exception.code,'sensitive')

    def test_external_redirect_never_receives_cookie(self):
        session=self.session(self.response(status=302,location='https://example.com/x'))
        with patch.object(account,'make_session',return_value=session),patch.object(account,'_browser_resolver',None),self.assertRaises(DownloadError) as error:
            account.resolve_authenticated(TARGET,'direct','',{'auth_token':'test-token'})
        self.assertEqual(error.exception.code,'account_access')
        session.get.assert_called_once()

    def test_dynamic_fallback_uses_only_the_requested_target(self):
        session=self.session(self.response('<html><body>Loading</body></html>'))
        fallback=MagicMock(); fallback.resolve.return_value='resolved'
        stop=threading.Event();cookies={'auth_token':'test-token'}
        with patch.object(account,'make_session',return_value=session),patch.object(account,'_browser_resolver',fallback):
            self.assertEqual(account.resolve_authenticated(TARGET,'manual','http://localhost:7890',cookies,stop),'resolved')
        fallback.resolve.assert_called_once_with(TARGET,'manual','http://localhost:7890',cookies,stop)

    def test_missing_account_fails_before_network(self):
        with patch.object(account,'make_session') as session,self.assertRaises(DownloadError) as error:
            account.resolve_authenticated(TARGET,'direct','',{})
        self.assertEqual(error.exception.code,'login_required');session.assert_not_called()

    def test_cancelled_task_fails_before_request(self):
        stop=threading.Event();stop.set();session=self.session()
        with patch.object(account,'make_session',return_value=session),self.assertRaises(DownloadError) as error:
            account.resolve_authenticated(TARGET,'direct','',{'auth_token':'test-token'},stop)
        self.assertEqual(error.exception.code,'interrupted');session.get.assert_not_called()

    def test_http_restriction_and_timeout_remain_specific(self):
        for status,code in [(401,'login_expired'),(403,'account_access'),(404,'not_found'),(429,'rate_limit')]:
            session=self.session(self.response(status=status))
            with self.subTest(status=status),patch.object(account,'make_session',return_value=session),patch.object(account,'_browser_resolver',None),self.assertRaises(DownloadError) as error:
                account.resolve_authenticated(TARGET,'direct','',{'auth_token':'test-token'})
            self.assertEqual(error.exception.code,code)
        session=self.session();session.get.side_effect=requests.exceptions.Timeout()
        with patch.object(account,'make_session',return_value=session),self.assertRaises(DownloadError) as error:
            account.resolve_authenticated(TARGET,'direct','',{'auth_token':'test-token'})
        self.assertEqual(error.exception.code,'timeout')

    def test_no_media_is_not_retried_in_browser(self):
        session=self.session(self.response(page(tweet_obj([]))))
        fallback=MagicMock()
        with patch.object(account,'make_session',return_value=session),patch.object(account,'_browser_resolver',fallback),self.assertRaises(DownloadError) as error:
            account.resolve_authenticated(TARGET,'direct','',{'auth_token':'test-token'})
        self.assertEqual(error.exception.code,'no_media');fallback.resolve.assert_not_called()

    def test_single_target_unavailable_payload_keeps_reason(self):
        payload={'data':{'tweetResult':{'result':{'__typename':'TweetUnavailable','reason':'Protected'}}}}
        with self.assertRaises(DownloadError) as error:account.parse_account_payload(payload,TARGET)
        self.assertEqual(error.exception.code,'private')

    def test_unrelated_media_never_returned(self):
        payload={'data':{'reply':tweet_obj(tweet_id='999999999')}}
        with self.assertRaises(DownloadError) as error:account.parse_account_payload(payload,TARGET)
        self.assertEqual(error.exception.code,'parse')


class BrowserLifecycleTests(unittest.TestCase):
    def test_cancelled_task_never_starts_browser(self):
        from xdownloader.browser_resolver import BrowserResolver
        stop=threading.Event();stop.set();resolver=BrowserResolver()
        with patch('xdownloader.webview.BrowserProcess') as host,self.assertRaises(DownloadError) as caught:
            resolver.resolve(TARGET,'direct','',{'auth_token':'test-token'},stop)
        self.assertEqual(caught.exception.code,'interrupted');host.assert_not_called()

    def test_close_terminates_active_hosts(self):
        from xdownloader.browser_resolver import BrowserResolver
        resolver=BrowserResolver();host=MagicMock();resolver.processes.add(host)
        resolver.shutdown()
        host.close.assert_called_once()
        with self.assertRaises(DownloadError):resolver.resolve(TARGET,'direct','',{})

    def test_unrelated_payload_does_not_complete_target(self):
        import queue
        from xdownloader.browser_resolver import BrowserResolver
        host=MagicMock();host.events=queue.Queue()
        host.events.put({'type':'payload','status':200,'payload':{'data':tweet_obj(tweet_id='999999999')}})
        host.events.put({'type':'payload','status':200,'payload':{'data':tweet_obj()}})
        resolver=BrowserResolver()
        with patch('xdownloader.webview.BrowserProcess',return_value=host):
            result=resolver.resolve(TARGET,'direct','',{'auth_token':'test-token'})
        self.assertEqual(result.tweet_id,TARGET.tweet_id)
        host.close.assert_called_once();self.assertFalse(resolver.processes)
