import io
import threading
import unittest
from unittest.mock import patch, MagicMock

from PIL import Image
from xdownloader.core import DownloadError, Tweet, Media, parse_page, normalize_url
from xdownloader.engine import Engine
from xdownloader.storage import Store
from tests.test_app import TARGET, folder, tweet_obj, page, FakeSession


class AuthorTests(unittest.TestCase):
    def test_authenticated_empty_results_are_unavailable_not_parse_errors(self):
        from xdownloader.account import parse_account_payload
        for payload in ({'data':{}},{'data':None},{'data':{'tweetResult':None}},{'data':{'tweetResult':{}}}):
            with self.subTest(payload=payload),self.assertRaises(DownloadError) as error:
                parse_account_payload(payload,TARGET)
            self.assertEqual(error.exception.code,'unavailable')

    def test_author_fields_split_across_core_and_legacy(self):
        obj=tweet_obj()
        obj['core']['user_results']['result']={'core':{'screen_name':'real_author'},'legacy':{'name':'实际名称'}}
        result=parse_page(page(obj),TARGET)
        self.assertEqual((result.username,result.display_name,result.warning),('real_author','实际名称',''))

    def test_author_wrapper_and_author_field(self):
        obj=tweet_obj()
        obj['author']={'result':obj.pop('core')['user_results']['result']}
        result=parse_page(page(obj),TARGET)
        self.assertEqual(result.display_name,'爱丽丝')
        self.assertEqual(result.warning,'')

    def test_matching_user_id_can_supply_missing_author(self):
        obj=tweet_obj(); obj['core']={}; obj['legacy']={'user_id_str':'90909'}
        user={'__typename':'User','rest_id':'90909','legacy':{'screen_name':'verified','name':'已核实'}}
        result=parse_page(page({'tweet':obj,'user':user}),TARGET)
        self.assertEqual((result.username,result.display_name,result.warning),('verified','已核实',''))

    def test_unrelated_author_never_used_as_fallback(self):
        obj=tweet_obj(); obj['core']={}
        other=tweet_obj(tweet_id='987654321')
        other['core']['user_results']['result']['core']={'screen_name':'wrong','name':'错误作者'}
        result=parse_page(page({'target':obj,'reply':other}),TARGET)
        self.assertEqual((result.username,result.display_name),(TARGET.username,'名称未知'))
        self.assertIn('未能',result.warning)

    def test_complete_duplicate_candidate_supplies_metadata(self):
        obj=tweet_obj(); obj['core']={}
        result=parse_page(page([obj,tweet_obj()]),TARGET)
        self.assertEqual(result.display_name,'爱丽丝')
        self.assertEqual(result.warning,'')

    def test_legacy_identity_and_partial_author(self):
        obj=tweet_obj(); del obj['rest_id']; obj['legacy']={'id_str':TARGET.tweet_id}
        obj['core']['user_results']['result']['core'].pop('name')
        result=parse_page(page(obj),TARGET)
        self.assertEqual(result.display_name,'名称未知')
        self.assertIn('显示名称',result.warning)

    def test_target_restrictions_are_classified_without_guessing(self):
        for reason,code in [('Suspended','suspended'),('Protected','private'),('AuthorOnly','private'),('Deleted','not_found'),('UnknownReason','unavailable')]:
            with self.subTest(reason=reason), self.assertRaises(DownloadError) as error:
                parse_page(page({'rest_id':TARGET.tweet_id,'result':{'__typename':'TweetUnavailable','unavailable_reason':reason}}),TARGET)
            self.assertEqual(error.exception.code,code)

    def test_unrelated_restriction_does_not_replace_target(self):
        result=parse_page(page({'target':tweet_obj(),'reply':{'rest_id':'999999','result':{'__typename':'TweetUnavailable','reason':'Protected'}}}),TARGET)
        self.assertEqual(len(result.media),2)

    def test_missing_author_download_finishes_with_persisted_warning(self):
        obj=tweet_obj([{'id_str':'m','type':'photo','media_url_https':'https://pbs.twimg.com/m.png'}]); obj['core']={}
        tweet=parse_page(page(obj),TARGET)
        store=Store(folder()); store.add_urls([TARGET],gif_mode='mp4')
        data=io.BytesIO(); Image.new('RGB',(8,8)).save(data,format='PNG')
        engine=Engine(store)
        try:
            with patch('xdownloader.engine.resolve',return_value=tweet), patch('xdownloader.engine.make_session',return_value=FakeSession(data.getvalue())):
                engine.run_tweet(TARGET.tweet_id)
            row=Store(store.base).tweet(TARGET.tweet_id)
            self.assertEqual(row['status'],'done')
            self.assertEqual(row['warning'],tweet.warning)
            media=store.media(TARGET.tweet_id)[0]
            self.assertTrue(store.media_path(media).is_file())
            self.assertIn('名称未知',media['filename'])
        finally:
            engine.shutdown()


class QueueDeletionTests(unittest.TestCase):
    def setUp(self):
        self.store=Store(folder())
        self.second=normalize_url('https://x.com/bob/status/987654321')
        self.store.add_urls([TARGET,self.second],gif_mode='mp4')
        self.engine=Engine(self.store)

    def tearDown(self):
        self.engine.shutdown()

    def test_delete_scopes_keep_files_and_release_duplicate_id(self):
        self.store.update_tweet(self.second.tweet_id,status='done')
        file=self.store.download/'keep.mp4'; file.write_bytes(b'keep')
        self.assertEqual(self.engine.delete_plans([TARGET.tweet_id,self.second.tweet_id]),1)
        self.assertIsNotNone(self.store.tweet(self.second.tweet_id))
        self.assertEqual(self.engine.delete_completed([self.second.tweet_id]),1)
        self.assertEqual(file.read_bytes(),b'keep')
        self.assertEqual(self.store.add_urls([TARGET]),([TARGET.tweet_id],0))

    def test_delete_during_resolve_cannot_recreate_plan(self):
        started,release=threading.Event(),threading.Event()
        def resolve(*args):
            started.set(); release.wait(5)
            return Tweet(TARGET.tweet_id,'alice','Alice',[Media('m',1,'photo','https://pbs.twimg.com/m.png','png')])
        with patch('xdownloader.engine.resolve',side_effect=resolve):
            self.engine.enqueue(TARGET.tweet_id)
            try:
                self.assertTrue(started.wait(5))
                future=self.engine.pending[TARGET.tweet_id]
                self.assertEqual(self.engine.delete_plans([TARGET.tweet_id]),1)
                self.assertEqual(self.store.tweet(TARGET.tweet_id)['status'],'deleting')
                self.assertFalse(self.engine.retry(TARGET.tweet_id,'anonymous'))
            finally:
                release.set()
            future.result(timeout=5)
        # shutdown joins the worker including its completion callback.
        self.engine.shutdown()
        self.assertIsNone(self.store.tweet(TARGET.tweet_id))
        self.assertEqual(self.store.media(TARGET.tweet_id),[])
        self.assertEqual(list(self.store.download.iterdir()),[])
        self.assertIsNotNone(self.store.tweet(self.second.tweet_id))

    def test_delete_running_transfer_stops_only_selected_task(self):
        started,release=threading.Event(),threading.Event()
        tweet=Tweet(TARGET.tweet_id,'alice','Alice',[Media('m',1,'photo','https://pbs.twimg.com/m.png','png')])
        partial=[]
        def fetch(tweet_id,mid,url,part,*args):
            part.write_bytes(b'partial'); partial.append(part)
            started.set(); release.wait(5)
            self.engine.check_stop(tweet_id)
        with patch('xdownloader.engine.resolve',return_value=tweet),patch.object(self.engine,'_fetch_part',side_effect=fetch):
            self.engine.enqueue(TARGET.tweet_id)
            try:
                self.assertTrue(started.wait(5))
                future=self.engine.pending[TARGET.tweet_id]
                self.engine.delete_plans([TARGET.tweet_id])
                self.assertFalse(self.engine.stop_signal(self.second.tweet_id).is_set())
            finally:
                release.set()
            future.result(timeout=5)
        self.engine.shutdown()
        self.assertIsNone(self.store.tweet(TARGET.tweet_id))
        self.assertEqual(partial[0].read_bytes(),b'partial')

    def test_restart_finishes_interrupted_deletion(self):
        self.store.save_tweet(Tweet(TARGET.tweet_id,'alice','Alice',[Media('m',1,'photo','https://pbs.twimg.com/m.png','png')]))
        self.store.update_tweet(TARGET.tweet_id,status='deleting')
        self.store.update_tweet(TARGET.tweet_id,status='done')
        self.assertEqual(self.store.tweet(TARGET.tweet_id)['status'],'deleting')
        restored=Store(self.store.base)
        self.assertIsNone(restored.tweet(TARGET.tweet_id))
        self.assertIsNotNone(restored.tweet(self.second.tweet_id))
