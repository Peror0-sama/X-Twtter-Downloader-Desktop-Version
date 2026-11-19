import json
import queue
import unittest
from unittest.mock import patch

import requests
from urllib3.exceptions import ReadTimeoutError

from xdownloader.account import parse_account_payload
from xdownloader.browser_resolver import BrowserResolver, page_status_script
from xdownloader.core import DownloadError, network_error, page_status_error, parse_page
from tests.test_app import TARGET, page, tweet_obj


class ResolutionErrorTests(unittest.TestCase):
    def run_events(self, events):
        clock = [0.0]
        pending = list(events)
        class Events:
            def get(self, timeout):
                clock[0] += 1 if pending else 30
                if pending:
                    return pending.pop(0)
                raise queue.Empty()
        class Process:
            def __init__(self, config):
                self.events = Events()
                self.config = config
            def send(self, command): pass
            def close(self): pass
        with patch('xdownloader.webview.BrowserProcess', Process), patch('xdownloader.browser_resolver.time.monotonic', side_effect=lambda: clock[0]):
            return BrowserResolver().resolve(TARGET, 'direct', '', {})

    def payload(self, obj=None):
        return {'type': 'payload', 'payload': {'data': tweet_obj() if obj is None else obj}}

    def status(self, text='', article=False, **extra):
        return {'type': 'page_status', 'result': json.dumps(dict(url=TARGET.url, has_article=article, text=text, **extra))}

    def test_later_media_wins_over_incomplete_empty_and_sensitive_results(self):
        for first in (
            self.payload({'__typename': 'Tweet', 'rest_id': TARGET.tweet_id}),
            self.payload(tweet_obj([])),
            {'type': 'payload', 'payload': {'data': {'tweetResult': {}}}},
            {'type': 'payload', 'payload': {'data': {'tweetResult': {'result': {'__typename': 'TweetUnavailable', 'reason': 'sensitive'}}}}},
            self.status('Something went wrong'),
            {'type': 'capture_error', 'target': True, 'detail': 'JsonException'},
        ):
            with self.subTest(first=first):
                result = self.run_events([first, self.payload()])
                self.assertEqual(len(result.media), 2)

    def test_error_cause_survives_later_initial_html(self):
        cases = [
            ({'type': 'capture_error', 'target': True, 'detail': 'JsonException'}, 'capture'),
            (self.payload({'unexpected': 1}), 'payload_parse'),
            (self.status('Something went wrong'), 'page_load'),
            ({'type': 'payload', 'status': 503}, 'api_http'),
            ({'type': 'payload', 'status': 403}, 'account_access'),
        ]
        for event, code in cases:
            with self.subTest(code=code), self.assertRaises(DownloadError) as caught:
                self.run_events([event, {'type': 'html', 'html': '<html>Loading</html>'}])
            self.assertEqual(caught.exception.code, code)

    def test_blank_page_is_not_reported_as_deleted_or_permission(self):
        with self.assertRaises(DownloadError) as caught:
            self.run_events([self.status(), {'type': 'html', 'html': '<html>Loading</html>'}])
        self.assertEqual(caught.exception.code, 'page_load')

    def test_tweet_body_error_words_do_not_classify_post(self):
        result = self.run_events([self.status('This Post was deleted', article=True), self.payload()])
        self.assertEqual(result.tweet_id, TARGET.tweet_id)

    def test_official_error_component_inside_target_article(self):
        with self.assertRaises(DownloadError) as caught:
            self.run_events([self.status(article=True, error_text='此貼文違反了 X 規則。了解更多')])
        self.assertEqual(caught.exception.code, 'rules')

    def test_capture_message_cannot_expose_raw_details(self):
        with self.assertRaises(DownloadError) as caught:
            self.run_events([{'type': 'capture_error', 'detail': 'auth_token=secret'}])
        self.assertEqual(caught.exception.code, 'capture')
        self.assertNotIn('secret', str(caught.exception))

    def test_non_target_capture_failure_is_ignored(self):
        result = self.run_events([{'type': 'capture_error', 'target': False}, self.payload()])
        self.assertEqual(len(result.media), 2)

    def test_empty_media_is_specific_after_waiting(self):
        with self.assertRaises(DownloadError) as caught:
            self.run_events([self.payload(tweet_obj([]))])
        self.assertEqual(caught.exception.code, 'no_media')

    def test_incomplete_tweet_is_not_no_media(self):
        with self.assertRaises(DownloadError) as caught:
            parse_page(page({'__typename': 'Tweet', 'rest_id': TARGET.tweet_id}), TARGET)
        self.assertEqual(caught.exception.code, 'parse')

    def test_single_target_response_does_not_accept_wrong_id(self):
        with self.assertRaises(DownloadError) as caught:
            parse_account_payload({'data': {'tweetResult': {'result': tweet_obj(tweet_id='999999999')}}}, TARGET)
        self.assertEqual(caught.exception.code, 'payload_parse')

    def test_user_text_cannot_close_json_script(self):
        obj = tweet_obj()
        obj['legacy'] = {'full_text': '</script><p>Something went wrong</p>'}
        self.assertEqual(len(parse_account_payload({'data': obj}, TARGET).media), 2)

    def test_malformed_target_response_reports_parse(self):
        with self.assertRaises(DownloadError) as caught:
            self.run_events([{'type': 'payload', 'payload': {'data': {'tweetResult': {'result': 'invalid'}}}}])
        self.assertEqual(caught.exception.code, 'payload_parse')

    def test_rules_api_reason(self):
        with self.assertRaises(DownloadError) as caught:
            parse_account_payload({'data': {'tweetResult': {'result': {'__typename': 'TweetTombstone', 'tombstone': {'text': 'This Post violated the X Rules.'}}}}}, TARGET)
        self.assertEqual(caught.exception.code, 'rules')

    def test_detail_tombstone_bound_to_exact_entry_not_recommendation(self):
        tombstone = {'__typename': 'TweetTombstone', 'tombstone': {'text': {'text': '此貼文違反了 X 規則。了解更多'}}}
        def entry(tid):
            return {'entryId': 'tweet-' + tid, 'content': {'itemContent': {'tweet_results': {'result': tombstone}}}}
        with self.assertRaises(DownloadError) as caught:
            parse_account_payload({'data': {'entries': [entry(TARGET.tweet_id)]}}, TARGET)
        self.assertEqual(caught.exception.code, 'rules')
        with self.assertRaises(DownloadError) as caught:
            parse_account_payload({'data': {'entries': [entry('999999999')]}}, TARGET)
        self.assertEqual(caught.exception.code, 'parse')

    def test_bounce_deleted_is_not_proof_of_author_deletion(self):
        with self.assertRaises(DownloadError) as caught:
            parse_account_payload({'data': {'tweetResult': {'result': {'__typename': 'TweetUnavailable', 'reason': 'BounceDeleted'}}}}, TARGET)
        self.assertEqual(caught.exception.code, 'unavailable')

    def test_network_failure_can_use_browser_without_retry_loop(self):
        from xdownloader import account
        from unittest.mock import MagicMock
        browser = MagicMock()
        browser.resolve.return_value = 'media'
        with patch.object(account, '_authenticated_page', side_effect=DownloadError('timeout', 'timeout')) as direct, patch.object(account, '_browser_resolver', browser):
            self.assertEqual(account.resolve_authenticated(TARGET, 'direct', '', {'auth_token': 'test'}), 'media')
        direct.assert_called_once()
        browser.resolve.assert_called_once()

    def test_network_error_identifies_stage_and_read_timeout(self):
        wrapped = requests.exceptions.ConnectionError(ReadTimeoutError(None, '/private', 'secret'))
        result = network_error(wrapped, '媒体下载失败')
        self.assertEqual(result.code, 'timeout')
        self.assertIn('读取', str(result))
        self.assertIn('媒体下载失败', str(result))
        self.assertNotIn('secret', str(result))
        self.assertIn('连接服务器超时', str(network_error(requests.exceptions.ConnectTimeout())))

    def test_visibility_wrapper_without_typename_and_media_is_no_media(self):
        result = {'__typename': 'TweetWithVisibilityResults', 'tweet': {
            'rest_id': TARGET.tweet_id, 'card': {'legacy': {'name': 'summary'}},
            'legacy': {'full_text': 'link', 'entities': {'urls': []}, 'user_id_str': '42'}}}
        with self.assertRaises(DownloadError) as caught:
            parse_account_payload({'data': {'tweetResult': {'result': result}}}, TARGET)
        self.assertEqual(caught.exception.code, 'external_card')
        self.assertIn('外链卡片', caught.exception.warning)

    def test_visibility_wrapper_media_is_not_blocked_by_label(self):
        obj = tweet_obj();obj.pop('__typename')
        result = {'__typename': 'TweetWithVisibilityResults', 'mediaVisibilityResults': {'reason': 'sensitive'}, 'tweet': obj}
        self.assertEqual(len(parse_account_payload({'data': {'tweetResult': {'result': result}}}, TARGET).media), 2)

    def test_external_card_fails_with_saved_warning_and_links_without_download(self):
        import tempfile
        from pathlib import Path
        from xdownloader.engine import Engine
        from xdownloader.storage import Store
        obj = tweet_obj([])
        obj['card'] = {'legacy': {'binding_values': [{'key': 'card_url', 'value': {'string_value': 'javascript:alert(1)'}}]}}
        obj['legacy'] = {'entities': {'urls': [
            {'expanded_url': 'https://example.com/media?id=42'},
            {'expanded_url': 'https://example.com/media?id=42'},
            {'expanded_url': 'https://user:password@example.com/private'}]}}
        with self.assertRaises(DownloadError) as caught:
            parse_account_payload({'data': obj}, TARGET)
        error = caught.exception
        self.assertEqual(error.code, 'external_card')
        self.assertEqual(error.warning.count('https://example.com/media?id=42'), 1)
        self.assertNotIn('javascript:', error.warning)
        self.assertNotIn('password', error.warning)
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory)); store.add_urls([TARGET])
            engine = Engine(store)
            try:
                with patch('xdownloader.engine.resolve', side_effect=error), patch.object(engine, '_fetch_part') as fetch:
                    engine.run_tweet(TARGET.tweet_id)
                fetch.assert_not_called()
                row = store.tweet(TARGET.tweet_id)
                self.assertEqual(row['status'], 'failed')
                self.assertEqual(row['error_code'], 'external_card')
                self.assertIn('https://example.com/media?id=42', row['warning'])
                self.assertFalse(list(store.download.iterdir()))
            finally:
                engine.shutdown()


if __name__ == '__main__':
    unittest.main()
