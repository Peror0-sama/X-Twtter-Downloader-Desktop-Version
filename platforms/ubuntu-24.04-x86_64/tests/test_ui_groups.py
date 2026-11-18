import os
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox, QScrollArea, QTabWidget, QWidget, QFileDialog
from xdownloader.core import normalize_url
from xdownloader.storage import Store
from xdownloader.ui import MainWindow, DownloadReport


def row(tweet_id, username="alice", status="done"):
    return {"id": tweet_id, "username": username, "display_name": "同一作者", "status": status,
            "url": f"https://x.com/{username}/status/{tweet_id}", "auth_mode": "anonymous", "error": "",
            "media": [{"media_id": "m" + tweet_id, "seq": 1, "kind": "video", "ext": "mp4",
                       "filename": tweet_id + ".mp4", "status": status, "total": 100,
                       "downloaded": 100, "speed": 0, "error": ""}]}


class GroupedQueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = Path(__file__).resolve().parents[1] / "work" / "ui-group-tests" / uuid.uuid4().hex
        self.window = MainWindow(Store(directory), interactive_close=False)
        self.window.timer.stop()

    def tearDown(self):
        self.window.close()
        self.app.processEvents()

    def test_creator_icon_used_for_window_and_tray(self):
        from xdownloader.ui import application_icon
        icon = application_icon()
        self.assertFalse(icon.isNull())
        for size in (16, 24, 32, 48, 256):
            self.assertFalse(icon.pixmap(size, size).isNull())
        self.assertFalse(self.window.windowIcon().isNull())
        self.assertFalse(self.window.tray.icon().isNull())
        self.assertEqual(self.window.windowIcon().cacheKey(), self.window.tray.icon().cacheKey())

    def test_close_choice_remembered_and_settings_can_change(self):
        from PySide6.QtGui import QCloseEvent
        w = self.window
        w.interactive_close = True
        try:
            with patch.object(w, 'ask_close_action', return_value='tray') as ask, patch.object(w, 'minimize_to_tray') as hide:
                event = QCloseEvent(); w.closeEvent(event)
                self.assertFalse(event.isAccepted())
                self.assertEqual(w.store.setting('close_action'), 'tray')
                w.closeEvent(QCloseEvent())
                ask.assert_called_once(); self.assertEqual(hide.call_count, 2)
            w.close_actions['exit'].trigger()
            self.assertEqual(w.store.setting('close_action'), 'exit')
            self.assertTrue(w.close_actions['exit'].isChecked())
            w.close_actions['ask'].trigger()
            self.assertEqual(w.store.setting('close_action'), 'ask')
        finally:
            w.interactive_close = False

    def test_tray_keeps_engine_running_and_restores_window(self):
        w = self.window
        w.show()
        with patch('xdownloader.ui.QSystemTrayIcon.isSystemTrayAvailable', return_value=True), patch.object(w.tray, 'show'):
            w.minimize_to_tray()
        self.assertTrue(w.in_tray)
        self.assertFalse(w.isVisible())
        self.assertFalse(w.engine.stop.is_set())
        w.restore_from_tray()
        self.assertFalse(w.in_tray)
        self.assertTrue(w.isVisible())

    def test_tray_completion_notifies_once_and_defers_format_prompt(self):
        w = self.window
        w.in_tray = True
        with patch.object(w.tray, 'showMessage') as notify:
            w.check_tray_completion([row('111111', status='downloading')])
            notify.assert_not_called()
            w.check_tray_completion([row('111111'), row('222222', status='waiting_choice')])
            w.check_tray_completion([row('111111'), row('222222', status='waiting_choice')])
            self.assertEqual(notify.call_count, 1)
            w.prompt_gif_format()
            self.assertIsNone(w.format_dialog)
            w.check_tray_completion([row('111111'), row('222222', status='failed')])
            self.assertEqual(notify.call_count, 2)
            self.assertIn('完成 1 条，失败 1 条', notify.call_args.args[1])
            w.check_tray_completion([row('111111'), row('222222', status='failed')])
            self.assertEqual(notify.call_count, 2)
        w.in_tray = False

    def test_direct_exit_while_downloading_requires_confirmation(self):
        from PySide6.QtGui import QCloseEvent
        w = self.window
        w.store.add_urls([normalize_url('https://x.com/alice/status/111111')])
        w.store.update_tweet('111111', status='downloading')
        w.set_close_action('exit')
        w.interactive_close = True
        try:
            with patch('xdownloader.ui.QMessageBox.question', return_value=QMessageBox.StandardButton.No):
                event = QCloseEvent(); w.closeEvent(event)
                self.assertFalse(event.isAccepted())
                self.assertFalse(w.closing)
                self.assertFalse(w.engine.stop.is_set())
            with patch('xdownloader.ui.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes), patch('xdownloader.ui.QTimer.singleShot'):
                event = QCloseEvent(); w.closeEvent(event)
                self.assertTrue(event.isAccepted())
                self.assertTrue(w.engine.closed)
                self.assertEqual(w.store.tweet('111111')['status'], 'queued')
        finally:
            w.interactive_close = False

    def test_same_account_grouped_case_insensitive_and_all_collapsed(self):
        tree = self.window.completed
        self.window.populate(tree, [row("111111", "alice"), row("222222", "Alice"), row("333333", "bob")])
        self.assertEqual(tree.topLevelItemCount(), 2)
        account = tree.topLevelItem(0)
        self.assertEqual(account.childCount(), 2)
        self.assertIn("2 帖", account.text(2))
        self.assertFalse(account.isExpanded())
        self.assertFalse(account.child(0).isExpanded())

    def test_refresh_preserves_user_expansion_but_new_post_is_collapsed(self):
        tree = self.window.completed
        items = [row("111111")]
        self.window.populate(tree, items)
        tree.topLevelItem(0).setExpanded(True)
        tree.topLevelItem(0).child(0).setExpanded(True)
        self.window.populate(tree, items + [row("222222")])
        account = tree.topLevelItem(0)
        self.assertTrue(account.isExpanded())
        self.assertTrue(account.child(0).isExpanded())
        self.assertFalse(account.child(1).isExpanded())
        account.setExpanded(False)
        self.window.populate(tree, items + [row("222222")])
        self.assertFalse(tree.topLevelItem(0).isExpanded())

    def test_completion_does_not_auto_expand_other_column(self):
        self.window.populate(self.window.active, [row("111111", status="downloading")])
        self.window.active.topLevelItem(0).setExpanded(True)
        self.window.populate(self.window.active, [])
        self.window.populate(self.window.completed, [row("111111")])
        self.assertFalse(self.window.completed.topLevelItem(0).isExpanded())
        self.assertFalse(self.window.completed.topLevelItem(0).child(0).isExpanded())

    def test_retry_account_only_retries_failed_posts_and_deduplicates_selection(self):
        tree = self.window.active
        items = [row("111111", status="failed"), row("222222", status="downloading")]
        self.window.populate(tree, items)
        account = tree.topLevelItem(0)
        account.setExpanded(True)
        account.setSelected(True)
        account.child(0).setSelected(True)
        by_id = {r["id"]: r for r in items}
        with patch.object(self.window.store, "tweet", side_effect=lambda key: by_id[key]), patch.object(self.window.engine, "retry") as retry, patch.object(self.window, "tick"):
            self.window.retry_selected(tree)
        retry.assert_called_once_with("111111", "anonymous")

    def test_account_open_does_not_lookup_fake_tweet_id(self):
        self.window.populate(self.window.completed, [row("111111")])
        with patch.object(self.window.store, "media") as media:
            self.window.open_item(self.window.completed.topLevelItem(0), True)
        media.assert_not_called()
        self.assertIn("1 条推文", self.window.detail.toPlainText())

    def test_prompt_only_when_needed_and_later_can_be_resumed(self):
        self.window.prompt_gif_format()
        self.assertIsNone(self.window.format_dialog)
        target = normalize_url('https://x.com/alice/status/5555555')
        self.window.store.add_urls([target])
        self.window.store.wait_for_format(target.tweet_id)
        self.window.tick()
        box = self.window.format_dialog
        self.assertIsNotNone(box)
        self.window.tick()
        self.assertIs(self.window.format_dialog, box)
        next(b for b in box.buttons() if b.text() == '稍后选择').click()
        self.app.processEvents()
        self.window.tick()
        self.assertIsNone(self.window.format_dialog)
        item = self.window.active.topLevelItem(0).child(0)
        self.window.double_click_item(item, False)
        box = self.window.format_dialog
        with patch.object(self.window.engine, 'enqueue'):
            next(b for b in box.buttons() if b.text() == '转换为 GIF').click()
            self.app.processEvents()
        self.assertIsNone(self.window.format_dialog)
        batch = self.window.store.tweet(target.tweet_id)['batch_id']
        self.assertEqual(self.window.store.batch_mode(batch), 'gif')
        self.window.store.choose_batch_format(batch, 'mp4')
        self.assertEqual(self.window.store.batch_mode(batch), 'gif')

    def test_clear_failed_preserves_other_tasks_and_downloaded_files(self):
        store = self.window.store
        for tid, status in [('111111','failed'),('222222','failed'),('333333','done'),('444444','downloading'),('555555','queued')]:
            store.add_urls([normalize_url(f'https://x.com/alice/status/{tid}')], gif_mode='mp4')
            store.update_tweet(tid,status=status)
        saved = store.download/'existing.mp4';saved.write_bytes(b'keep downloaded file')
        with patch.object(self.window.engine,'enqueue'),patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.No):
            self.window.tick()
            self.assertTrue(self.window.clear_failed_button.isEnabled())
            self.window.clear_failed()
            self.assertEqual(len(store.snapshot()),5)
        with patch.object(self.window.engine,'enqueue'),patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.Yes):
            self.window.clear_failed()
        self.assertEqual({t['id'] for t in store.snapshot()},{'333333','444444','555555'})
        self.assertEqual(saved.read_bytes(),b'keep downloaded file')
        self.assertFalse(self.window.clear_failed_button.isEnabled())
        self.assertEqual(store.add_urls([normalize_url('https://x.com/alice/status/111111')],gif_mode='mp4'),(['111111'],0))

    def test_start_waiting_post_prompts_its_batch_only(self):
        targets = [normalize_url(f'https://x.com/alice/status/{tid}') for tid in ('111111','222222')]
        batches=[]
        for target in targets:
            self.window.store.add_urls([target]);self.window.store.wait_for_format(target.tweet_id)
            batch=self.window.store.tweet(target.tweet_id)['batch_id'];batches.append(batch)
            self.window.dismissed_batches.add(batch)
        self.window.start_queue_items({targets[1].tweet_id})
        box=self.window.format_dialog
        self.assertIsNotNone(box)
        with patch.object(self.window.engine,'enqueue'):
            next(b for b in box.buttons() if b.text()=='转换为 GIF').click()
            self.app.processEvents()
        self.assertIsNone(self.window.store.batch_mode(batches[0]))
        self.assertEqual(self.window.store.batch_mode(batches[1]),'gif')

    def test_individual_dialog_can_defer_and_choose_different_formats(self):
        from xdownloader.core import Tweet,Media
        from xdownloader.ui import IndividualGifDialog
        target=normalize_url('https://x.com/alice/status/111111')
        store=self.window.store;store.add_urls([target],gif_mode='individual')
        store.save_tweet(Tweet(target.tweet_id,'alice','Alice',[
            Media('one',1,'animated_gif','https://video.twimg.com/a.mp4','mp4'),
            Media('two',2,'animated_gif','https://video.twimg.com/b.mp4','mp4')]))
        store.wait_for_format(target.tweet_id);self.window.tick()
        box=self.window.format_dialog;self.assertIsInstance(box,IndividualGifDialog)
        self.app.processEvents()
        for combo in box.choices.values():
            self.assertGreaterEqual(combo.height(), combo.sizeHint().height())
        box.reject();self.app.processEvents();self.assertIsNone(self.window.format_dialog)
        self.window.start_queue_items({target.tweet_id})
        box=self.window.format_dialog;box.choices['two'].setCurrentIndex(1)
        with patch.object(self.window.engine,'enqueue'):
            box.accept();self.app.processEvents()
        self.assertEqual([m['gif_mode'] for m in store.media(target.tweet_id)],['mp4','gif'])
        self.assertIsNone(self.window.format_dialog)

    def test_batch_prompt_offers_individual_mode(self):
        target=normalize_url('https://x.com/alice/status/111111')
        store=self.window.store;store.add_urls([target]);store.wait_for_format(target.tweet_id)
        self.window.prompt_gif_format()
        box=self.window.format_dialog
        with patch.object(self.window.engine,'enqueue'):
            next(b for b in box.buttons() if b.text()=='逐个选择').click();self.app.processEvents()
        self.assertEqual(store.batch_mode(store.tweet(target.tweet_id)['batch_id']),'individual')

    def test_history_button_cancellation_preserves_records(self):
        target = normalize_url('https://x.com/alice/status/5555555')
        self.window.store.add_urls([target])
        self.window.store.update_tweet(target.tweet_id, status='done')
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.No):
            self.window.clear_history()
        self.assertIsNotNone(self.window.store.tweet(target.tweet_id))
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes):
            self.window.clear_history()
        self.assertIsNone(self.window.store.tweet(target.tweet_id))

    def test_small_window_keeps_fixed_sections_without_outer_scroll(self):
        self.window.show()
        self.window.resize(1120, 720)
        tabs = self.window.findChild(QTabWidget)
        for tab in range(2):
            tabs.setCurrentIndex(tab)
            self.app.processEvents()
            self.assertEqual(self.window.findChildren(QScrollArea), [])
            for control in self.window.findChildren(QWidget):
                if control.isVisible() and control.parentWidget() and control.width() > 1 and control.height() > 1 and not control.inherits('QScrollBar'):
                    self.assertTrue(control.parentWidget().rect().contains(control.geometry()), control.objectName())

    def test_input_auto_newline_preserves_unicode_cursor_and_undo(self):
        field=self.window.input
        first='https://x.com/alice/status/111111'
        second='https://x.com/bob/status/222222'
        field.insertPlainText('😀'+first+second)
        self.app.processEvents()
        expected='😀\n'+first+'\n'+second
        self.assertEqual(field.toPlainText(),expected)
        self.assertEqual(field.textCursor().position(),len(expected.encode('utf-16-le'))//2)
        field.undo()
        self.app.processEvents()
        self.assertEqual(field.toPlainText(),'')
        field.redo()
        self.app.processEvents()
        self.assertEqual(field.toPlainText(),expected)
        field.setPlainText(first+'\n'+second)
        self.app.processEvents()
        self.assertEqual(field.toPlainText(),first+'\n'+second)

    def test_compact_actions_share_row_and_static_hints_removed(self):
        from PySide6.QtWidgets import QPushButton,QLabel
        self.window.show();self.window.resize(1120,720);self.app.processEvents()
        buttons={b.text():b for b in self.window.findChildren(QPushButton)}
        controls=[buttons[name] for name in ('重试所选失败','重试全部失败','清除所有失败')]
        self.assertEqual(len({b.y() for b in controls}),1)
        self.assertTrue(all(b.width()>=b.minimumSizeHint().width() for b in controls))
        labels=[v.text() for v in self.window.findChildren(QLabel)]
        self.assertIn('详细信息',labels)
        self.assertNotIn('详细信息 · 可拖动上方分隔条调整高度',labels)
        self.assertEqual(self.window.import_label.text(),'')

    def test_directory_picker_and_cancel(self):
        original = self.window.store.download
        with patch.object(QFileDialog, 'getExistingDirectory', return_value=''):
            self.window.choose_download_directory()
        self.assertEqual(self.window.store.download, original)
        chosen = self.window.store.base / 'chosen directory'
        with patch.object(QFileDialog, 'getExistingDirectory', return_value=str(chosen)):
            self.window.choose_download_directory()
        self.assertEqual(self.window.store.download, chosen)
        self.assertIn(str(chosen), self.window.folder_label.text())

    def test_open_media_reveals_original_file_after_directory_change(self):
        from xdownloader.core import Tweet, Media
        target = normalize_url('https://x.com/alice/status/5555555')
        store = self.window.store
        store.add_urls([target])
        store.save_tweet(Tweet(target.tweet_id,'alice','Alice',[Media('m',1,'photo','https://pbs.twimg.com/m.png','png')]))
        media = store.media(target.tweet_id)[0]
        path = store.media_path(media)
        path.write_bytes(b'existing test file')
        store.update_media(target.tweet_id,'m',status='done')
        store.update_tweet(target.tweet_id,status='done')
        store.set_download_directory(store.base / 'new folder')
        self.window.populate(self.window.completed,store.snapshot())
        item = self.window.completed.topLevelItem(0).child(0).child(0)
        with patch('xdownloader.linux_support.reveal_file') as launch, patch('xdownloader.ui.QDesktopServices.openUrl') as open_url:
            self.window.open_item(item,True)
        launch.assert_called_once_with(path)
        self.assertNotIn('shell',launch.call_args.kwargs)
        open_url.assert_not_called()

    def test_directory_picker_updates_queued_file_and_its_display(self):
        from xdownloader.core import Tweet, Media
        store = self.window.store
        target = normalize_url('https://x.com/alice/status/5555555')
        store.add_urls([target])
        store.save_tweet(Tweet(target.tweet_id,'alice','Alice',[Media('m',1,'photo','https://pbs.twimg.com/m.png','png')]))
        chosen = store.base/'new destination'
        with patch.object(QFileDialog,'getExistingDirectory',return_value=str(chosen)), patch.object(self.window.engine,'enqueue'):
            self.window.choose_download_directory()
        media = store.media(target.tweet_id)[0]
        self.assertEqual(store.media_path(media).parent,chosen)
        item = self.window.active.topLevelItem(0).child(0).child(0)
        self.assertIn(str(chosen),item.data(0,Qt.ItemDataRole.UserRole+1))

    def test_post_with_files_in_two_directories_prompts_to_select_file(self):
        from xdownloader.core import Tweet, Media
        store = self.window.store
        target = normalize_url('https://x.com/alice/status/5555555')
        store.add_urls([target])
        store.save_tweet(Tweet(target.tweet_id,'alice','Alice',[
            Media(str(i),i,'photo',f'https://pbs.twimg.com/{i}.png','png') for i in (1,2)]))
        store.begin_media(target.tweet_id,'1')
        store.set_download_directory(store.base/'new folder')
        for mid in ('1','2'):
            store.update_media(target.tweet_id,mid,status='done')
        store.update_tweet(target.tweet_id,status='done')
        self.window.populate(self.window.completed,store.snapshot())
        post = self.window.completed.topLevelItem(0).child(0)
        with patch.object(QMessageBox,'information') as info, patch.object(self.window,'open_directory') as open_dir:
            self.window.open_item(post,True)
        info.assert_called_once()
        open_dir.assert_not_called()

    def test_active_context_menu_limits_author_scope_to_unfinished_posts(self):
        store=self.window.store
        targets=[normalize_url(f'https://x.com/{user}/status/{tid}') for user,tid in [('alice','111111'),('Alice','222222'),('bob','333333'),('alice','444444')]]
        store.add_urls(targets)
        for target in targets:
            store.update_tweet(target.tweet_id,status='done' if target.tweet_id=='444444' else 'failed')
        self.window.populate(self.window.active,[t for t in store.snapshot() if t['status']!='done'])
        item=self.window.active.topLevelItem(0).child(0)
        menu=self.window.make_queue_menu(self.window.active,item,False)
        actions={a.text():a for a in menu.actions() if not a.isSeparator()}
        self.assertEqual(set(actions),{'开始下载','删除下载计划'})
        with patch.object(self.window,'delete_queue_items') as delete:
            actions['删除下载计划'].trigger()
            delete.assert_called_once_with({'111111'},False)
        menu.deleteLater()
        menu=self.window.make_queue_menu(self.window.active,item.parent(),False)
        actions={a.text():a for a in menu.actions() if not a.isSeparator()}
        self.assertEqual(set(actions),{'开始下载该作者所有','删除该作者所有下载计划'})
        with patch.object(self.window,'start_queue_items') as start:
            actions['开始下载该作者所有'].trigger()
            start.assert_called_once_with({'111111','222222'})
        with patch.object(self.window,'delete_queue_items') as delete:
            actions['删除该作者所有下载计划'].trigger()
            delete.assert_called_once_with({'111111','222222'},False)
        menu.deleteLater()

    def test_completed_context_delete_keeps_files_and_other_records(self):
        store=self.window.store
        target=normalize_url('https://x.com/alice/status/111111')
        store.add_urls([target]); store.update_tweet(target.tweet_id,status='done')
        path=store.download/'keep.mp4'; path.write_bytes(b'keep')
        self.window.populate(self.window.completed,store.snapshot())
        item=self.window.completed.topLevelItem(0).child(0)
        menu=self.window.make_queue_menu(self.window.completed,item,True)
        self.assertEqual([a.text() for a in menu.actions() if not a.isSeparator()],['打开文件所在目录','打开文件','删除已完成记录'])
        next(a for a in menu.actions() if a.text()=='删除已完成记录').trigger()
        self.assertIsNone(store.tweet(target.tweet_id))
        self.assertEqual(path.read_bytes(),b'keep')
        menu.deleteLater()

    def test_active_double_click_retries_post_without_opening_file(self):
        self.window.populate(self.window.active,[row('111111',status='failed')])
        item=self.window.active.topLevelItem(0).child(0).child(0)
        with patch.object(self.window,'start_queue_items') as start,patch.object(self.window,'open_item') as opened:
            self.window.active.itemDoubleClicked.emit(item,0)
        start.assert_called_once_with({'111111'})
        opened.assert_not_called()
        self.assertFalse(self.window.active.expandsOnDoubleClick())

    def test_completed_warning_is_visible_and_details_can_expand(self):
        tweet=row('111111'); tweet['warning']='未能取得作者显示名称，文件名暂用“名称未知”'
        self.window.populate(self.window.completed,[tweet])
        account=self.window.completed.topLevelItem(0); post=account.child(0)
        self.assertIn('警告',account.text(1))
        self.assertIn('警告',post.text(1))
        self.assertIn(tweet['warning'],post.child(0).data(0,Qt.ItemDataRole.UserRole+1))
        self.window.show(); self.app.processEvents()
        before=self.window.detail.height()
        self.window.queue_splitter.setSizes([250,350]); self.app.processEvents()
        self.assertGreaterEqual(before,100)
        self.assertGreater(self.window.detail.height(),before)

    def test_report_classifies_each_post_once_including_failure_with_warning(self):
        failed=row('111111',status='failed'); failed['warning']='作者信息不完整'; failed['error']='无权访问'
        warned=row('222222'); warned['warning']='名称未知'
        success=row('333333')
        dialog=DownloadReport([failed,warned,success],self.window)
        try:
            self.assertEqual([dialog.tabs.tabText(i) for i in range(3)],['失败（1）','警告（1）','成功（1）'])
            for title,expected in [('失败',failed),('警告',warned),('成功',success)]:
                table=dialog.tables[title]
                self.assertEqual(table.rowCount(),1)
                self.assertEqual(table.item(0,0).text(),expected['url'])
            self.assertIn('无权访问',dialog.tables['失败'].item(0,3).text())
            self.assertIn('作者信息不完整',dialog.tables['失败'].item(0,3).text())
        finally:
            dialog.close()

    def test_report_becomes_available_only_after_all_work_is_finished(self):
        store=self.window.store
        target=normalize_url('https://x.com/alice/status/111111')
        store.add_urls([target]); store.wait_for_format(target.tweet_id)
        with patch.object(self.window,'prompt_gif_format'):
            self.window.tick()
        self.assertFalse(self.window.report_button.isEnabled())
        store.update_tweet(target.tweet_id,status='failed',error='测试失败')
        with patch.object(self.window.engine,'busy',return_value=True):
            self.window.tick()
            self.assertFalse(self.window.report_button.isEnabled())
        self.window.tick()
        self.assertTrue(self.window.report_button.isEnabled())
        self.window.show_report()
        self.assertEqual(self.window.report_dialog.tables['失败'].rowCount(),1)
        self.window.report_dialog.close()

    def test_report_remains_readable_with_system_dark_palette(self):
        from PySide6.QtGui import QPalette, QColor
        original = self.app.palette()
        dark = QPalette(original)
        for role, color in [(QPalette.ColorRole.Window, '#202020'),
                            (QPalette.ColorRole.Base, '#2b2b2b'),
                            (QPalette.ColorRole.AlternateBase, '#383838'),
                            (QPalette.ColorRole.WindowText, '#eeeeee'),
                            (QPalette.ColorRole.Text, '#eeeeee')]:
            dark.setColor(role, QColor(color))
        report = None
        try:
            self.app.setPalette(dark)
            failed = row('111111', status='failed')
            failed['error'] = 'X 未返回此推文：可能已删除或当前账号无权查看'
            report = DownloadReport([failed, row('222222')], self.window)
            report.show(); self.app.processEvents()
            table = report.tables['失败']
            self.assertEqual(report.palette().color(QPalette.ColorRole.Window).name(), '#f3f5f9')
            self.assertEqual(table.viewport().palette().color(QPalette.ColorRole.Base).name(), '#ffffff')
            self.assertEqual(table.palette().color(QPalette.ColorRole.Text).name(), '#203047')
            from PySide6.QtWidgets import QLabel
            for label in report.findChildren(QLabel):
                self.assertEqual(label.palette().color(QPalette.ColorRole.WindowText).name(), '#203047')
            # 验证真正绘制的空白单元区域，不只检查样式字符串。
            image = table.viewport().grab().toImage()
            self.assertGreater(image.pixelColor(image.width()-10, image.height()-10).lightness(), 235)
        finally:
            if report: report.close()
            self.app.setPalette(original)

    def test_context_open_file_and_folder_use_distinct_original_paths(self):
        from xdownloader.core import Tweet,Media
        store=self.window.store
        target=normalize_url('https://x.com/alice/status/111111')
        store.add_urls([target]); store.save_tweet(Tweet(target.tweet_id,'alice','Alice',[Media('m',1,'photo','https://pbs.twimg.com/a.png','png')]))
        media=store.begin_media(target.tweet_id,'m'); path=store.media_path(media); path.write_bytes(b'test')
        store.update_media(target.tweet_id,'m',status='done'); store.update_tweet(target.tweet_id,status='done')
        store.set_download_directory(store.base/'new folder')
        self.window.populate(self.window.completed,store.snapshot())
        item=self.window.completed.topLevelItem(0).child(0).child(0)
        menu=self.window.make_queue_menu(self.window.completed,item,True)
        actions={a.text():a for a in menu.actions() if not a.isSeparator()}
        self.assertTrue(actions['打开文件'].isEnabled())
        with patch('xdownloader.ui.QDesktopServices.openUrl',return_value=True) as opened:
            actions['打开文件'].trigger()
            self.assertEqual(opened.call_args.args[0].toLocalFile(),str(path).replace('\\','/'))
        with patch.object(self.window,'open_directory') as folder:
            actions['打开文件所在目录'].trigger()
            folder.assert_called_once_with(path.parent)
        path.unlink()
        with patch('xdownloader.ui.QDesktopServices.openUrl') as opened,patch.object(QMessageBox,'warning') as warning:
            actions['打开文件'].trigger()
            opened.assert_not_called();warning.assert_called_once()
        menu.deleteLater()

    def test_open_file_disabled_for_parent_with_multiple_media(self):
        from xdownloader.core import Tweet,Media
        store=self.window.store
        target=normalize_url('https://x.com/alice/status/111111')
        store.add_urls([target]); store.save_tweet(Tweet(target.tweet_id,'alice','Alice',[
            Media(str(i),i,'photo',f'https://pbs.twimg.com/{i}.png','png') for i in (1,2)]))
        for mid in ('1','2'):store.update_media(target.tweet_id,mid,status='done')
        store.update_tweet(target.tweet_id,status='done')
        self.window.populate(self.window.completed,store.snapshot())
        item=self.window.completed.topLevelItem(0).child(0)
        menu=self.window.make_queue_menu(self.window.completed,item,True)
        self.assertFalse(next(a for a in menu.actions() if a.text()=='打开文件').isEnabled())
        menu.deleteLater()

    def test_author_completed_menu_opens_only_its_files_and_continues_after_missing(self):
        from xdownloader.core import Tweet,Media
        store=self.window.store
        for user,tid,status in [('alice','111111','done'),('Alice','222222','done'),('bob','333333','done'),('alice','444444','failed')]:
            target=normalize_url(f'https://x.com/{user}/status/{tid}')
            store.add_urls([target])
            store.save_tweet(Tweet(tid,user,user,[Media('m',1,'photo','https://pbs.twimg.com/a.png','png')]))
            media=store.begin_media(tid,'m')
            if tid!='111111':store.media_path(media).write_bytes(b'test')
            store.update_media(tid,'m',status=status);store.update_tweet(tid,status=status)
        self.window.populate(self.window.completed,[t for t in store.snapshot() if t['status']=='done'])
        author=self.window.completed.topLevelItem(0)
        menu=self.window.make_queue_menu(self.window.completed,author,True)
        actions={a.text():a for a in menu.actions() if not a.isSeparator()}
        self.assertEqual(set(actions),{'打开文件所在目录','打开该作者所有文件','删除该作者所有已完成记录'})
        with patch('xdownloader.ui.QDesktopServices.openUrl',return_value=True) as opened,patch.object(QMessageBox,'warning') as warning:
            actions['打开该作者所有文件'].trigger()
            opened.assert_called_once()
            self.assertEqual(opened.call_args.args[0].toLocalFile(),str(store.media_path(store.media('222222')[0])).replace('\\','/'))
            warning.assert_called_once()
        store.media_path(store.media('111111')[0]).write_bytes(b'test')
        with patch('xdownloader.ui.QDesktopServices.openUrl',return_value=True) as opened:
            actions['打开该作者所有文件'].trigger()
            self.assertEqual({call.args[0].toLocalFile() for call in opened.call_args_list},
                             {str(store.media_path(store.media(tid)[0])).replace('\\','/') for tid in ('111111','222222')})
            self.assertEqual(opened.call_count,2)
        with patch.object(self.window,'delete_queue_items') as delete:
            actions['删除该作者所有已完成记录'].trigger()
            delete.assert_called_once_with({'111111','222222'},True)
        menu.deleteLater()

    def test_mixed_selection_does_not_apply_author_scope_to_single_item_menu(self):
        tree=self.window.active
        self.window.populate(tree,[row('111111',status='failed'),row('222222',status='failed')])
        author=tree.topLevelItem(0);author.setExpanded(True)
        author.setSelected(True);author.child(0).setSelected(True)
        menu=self.window.make_queue_menu(tree,author.child(0),False)
        with patch.object(self.window,'start_queue_items') as start:
            next(a for a in menu.actions() if a.text()=='开始下载').trigger()
            start.assert_called_once_with({'111111'})
        menu.deleteLater()


if __name__ == "__main__":
    unittest.main()
