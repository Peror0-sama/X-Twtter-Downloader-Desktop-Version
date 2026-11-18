import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from xdownloader.core import Media, Tweet
from xdownloader.engine import Engine, verify_file
from xdownloader.storage import Store
from xdownloader.ui import MainWindow, PROGRESS_ROLE, queue_progress
from tests.test_app import TARGET
from tests.test_ui_groups import row


class ProgressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])

    def test_plan_and_collapsed_author_include_partial_media(self):
        with tempfile.TemporaryDirectory() as d:
            window=MainWindow(Store(Path(d)),interactive_close=False);window.timer.stop()
            try:
                tweet=row('123456789',status='downloading')
                tweet['media'][0].update(status='done')
                tweet['media'].append(dict(tweet['media'][0],media_id='second',seq=2,status='downloading',downloaded=0))
                window.populate(window.active,[tweet])
                account=window.active.topLevelItem(0);plan=account.child(0)
                self.assertFalse(account.isExpanded())
                self.assertEqual(account.data(2,PROGRESS_ROLE)[0],50)
                self.assertEqual(plan.data(2,PROGRESS_ROLE)[0],50)
                self.assertEqual(plan.child(0).data(2,PROGRESS_ROLE)[0],100)
                self.assertEqual(plan.child(1).data(2,PROGRESS_ROLE)[0],0)
                plan.setSelected(True);account.setExpanded(True)
                tweet['media'][1]['downloaded']=50
                window.populate(window.active,[tweet])
                self.assertTrue(window.active.topLevelItem(0).isExpanded())
                self.assertTrue(window.active.topLevelItem(0).child(0).isSelected())
                self.assertEqual(window.active.topLevelItem(0).child(0).data(2,PROGRESS_ROLE)[0],75)
            finally:
                window.close();self.app.processEvents()

    def test_unknown_total_and_pending_do_not_claim_percentage(self):
        media=dict(status='downloading',downloaded=2048,total=0)
        value,label,_=queue_progress([media],'downloading')
        self.assertIsNone(value);self.assertIn('2.0 KB',label);self.assertNotIn('%',label)
        self.assertEqual(queue_progress([], 'queued')[:2],(0,'等待中'))
        self.assertEqual(queue_progress([], 'resolving')[:2],(None,'解析中'))

    def test_download_merge_verify_and_failure_never_claim_completion(self):
        media=dict(status='downloading',downloaded=100,total=100)
        self.assertEqual(queue_progress([media],'downloading')[0],99)
        media.update(total=0,_segments_done=7,_segments_total=10)
        self.assertEqual(queue_progress([media],'downloading')[0],70)
        media.update(_segments_done=10,_phase='merging')
        self.assertEqual(queue_progress([media],'downloading')[:2],(99,'99% · 合并中'))
        media['_phase']='verifying'
        self.assertIn('校验',queue_progress([media],'downloading')[1])
        self.assertIn('失败',queue_progress([media],'failed')[1])
        media['status']='done'
        self.assertEqual(queue_progress([media],'done')[0],100)

    def test_progress_snapshot_is_isolated_and_prunes_deleted_records(self):
        with tempfile.TemporaryDirectory() as d:
            engine=Engine(Store(Path(d)))
            try:
                engine.set_transfer_progress('one','m',_segments_done=3)
                snapshot=engine.progress_snapshot();snapshot[('one','m')]['_segments_done']=9
                self.assertEqual(engine.progress_snapshot()[('one','m')]['_segments_done'],3)
                self.assertEqual(engine.progress_snapshot(set()),{})
            finally:engine.shutdown()


class HlsProgressIntegration(unittest.TestCase):
    def test_real_segments_merge_and_engine_completion(self):
        from xdownloader.conversion import ffmpeg_path
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);source=base/'segments';source.mkdir()
            subprocess.run([str(ffmpeg_path()),'-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc=size=64x64:rate=10','-t','4','-c:v','mpeg2video','-g','10','-sc_threshold','0','-f','hls','-hls_time','1','-hls_list_size','0','-hls_segment_filename',str(source/'%02d.ts'),str(source/'index.m3u8')],check=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
            paths=sorted(source.glob('*.ts'));self.assertGreater(len(paths),1)
            urls=['https://video.twimg.com/'+p.name for p in paths]
            store=Store(base/'app');store.add_urls([TARGET],gif_mode='mp4')
            engine=Engine(store)
            events=[]
            update=engine.set_transfer_progress
            def record(tid,mid,**values):
                events.append(dict(values));update(tid,mid,**values)
            def fetch(url,*args,**kwargs):return (source/url.rsplit('/',1)[-1]).read_bytes(),url
            tweet=Tweet(TARGET.tweet_id,'alice','Alice',[Media('m',1,'video','https://video.twimg.com/master.m3u8','mp4')])
            try:
                with patch('xdownloader.engine.resolve',return_value=tweet),patch('xdownloader.hls.media_playlist',return_value=(urls,4)),patch('xdownloader.hls.get_bytes',side_effect=fetch),patch.object(engine,'set_transfer_progress',side_effect=record):
                    engine.run_tweet(TARGET.tweet_id)
                self.assertEqual(store.tweet(TARGET.tweet_id)['status'],'done')
                self.assertEqual([e['_segments_done'] for e in events if e.get('_phase')=='downloading'],list(range(len(paths)+1)))
                self.assertIn('merging',[e['_phase'] for e in events])
                self.assertEqual(events[-1]['_phase'],'verifying')
                media=store.media(TARGET.tweet_id)[0]
                self.assertEqual(verify_file(store.media_path(media))[0],'mp4')
                self.assertEqual(queue_progress([media],'done')[0],100)
            finally:engine.shutdown()
