import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from unittest.mock import MagicMock, patch
from PySide6.QtCore import QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit
from xdownloader.login import LoginDialog


class AddressLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        account=MagicMock();account.cookies.return_value={}
        self.stub=patch.object(LoginDialog,'start_browser');self.stub.start()
        self.dialog=LoginDialog(account,'system','')
        self.dialog.send=MagicMock()
        self.other=QLineEdit(self.dialog);self.dialog.layout().addWidget(self.other)
        self.dialog.show();self.dialog.activateWindow();self.app.processEvents()
        self.dialog.send.reset_mock()
        self.address=self.dialog.address

    def tearDown(self):
        self.dialog.close();self.dialog.deleteLater();self.app.processEvents();self.stub.stop()

    def unlock_and_edit(self):
        QTest.mouseClick(self.address.cover,Qt.MouseButton.LeftButton)
        QTest.mouseClick(self.address,Qt.MouseButton.LeftButton)

    def test_default_lock_and_first_click_do_not_accept_keys_or_navigation(self):
        original=self.address.text()
        self.assertTrue(self.address.isReadOnly())
        self.assertFalse(self.address.hasFocus())
        QTest.mouseClick(self.address.cover,Qt.MouseButton.LeftButton)
        self.assertEqual(self.address.edit_state,'armed')
        self.assertTrue(self.address.isReadOnly())
        self.assertFalse(self.address.hasFocus())
        QTest.keyClicks(self.address,'sample-login-text')
        QTest.keyClick(self.address,Qt.Key.Key_Return)
        self.assertEqual(self.address.text(),original)
        self.dialog.send.assert_not_called()

    def test_second_click_enables_typing_and_enter_navigates_then_locks(self):
        self.unlock_and_edit()
        self.assertFalse(self.address.isReadOnly())
        self.assertTrue(self.address.hasFocus())
        self.address.selectAll();QTest.keyClicks(self.address,'https://x.com/example')
        QTest.keyClick(self.address,Qt.Key.Key_Return)
        self.dialog.send.assert_called_once_with({'op':'navigate','url':'https://x.com/example'})
        self.assertEqual(self.address.edit_state,'locked')
        self.assertTrue(self.address.cover.isVisible())
        self.assertFalse(self.address.hasFocus())

    def test_other_input_receives_text_without_modifying_address(self):
        self.unlock_and_edit()
        self.address.selectAll();QTest.keyClicks(self.address,'unsubmitted-draft')
        QTest.mouseClick(self.other,Qt.MouseButton.LeftButton)
        QTest.keyClicks(self.other,'sample-user')
        self.assertEqual(self.other.text(),'sample-user')
        self.assertEqual(self.address.text(),'https://x.com/i/flow/login')
        self.assertEqual(self.address.edit_state,'locked')
        self.dialog.send.assert_not_called()

    def test_browser_updates_do_not_replace_active_draft(self):
        self.unlock_and_edit()
        self.address.selectAll();QTest.keyClicks(self.address,'https://x.com/draft')
        self.address.set_page_url('https://x.com/home')
        self.assertEqual(self.address.text(),'https://x.com/draft')
        QTest.mouseClick(self.other,Qt.MouseButton.LeftButton)
        self.assertEqual(self.address.text(),'https://x.com/home')

    def test_window_deactivation_relocks_armed_address(self):
        QTest.mouseClick(self.address.cover,Qt.MouseButton.LeftButton)
        QApplication.sendEvent(self.dialog,QEvent(QEvent.Type.WindowDeactivate))
        self.assertEqual(self.address.edit_state,'locked')
        self.assertTrue(self.address.isReadOnly())

    def test_tab_cannot_enter_locked_or_armed_address(self):
        for armed in (False,True):
            if armed:QTest.mouseClick(self.address.cover,Qt.MouseButton.LeftButton)
            self.other.setFocus()
            for _ in range(8):
                QTest.keyClick(self.app.focusWidget(),Qt.Key.Key_Tab)
                self.assertFalse(self.address.hasFocus())

    def test_embedded_browser_focus_notification_relocks_address(self):
        import queue
        self.unlock_and_edit()
        process=MagicMock();process.events=queue.Queue()
        process.events.put({'type':'browser_focus'})
        self.dialog.process=process
        self.dialog.poll()
        self.dialog.send.assert_called_with({'op': 'focus'})
        self.assertEqual(self.address.edit_state,'locked')
        self.assertFalse(self.address.hasFocus())
        self.dialog.process=None
