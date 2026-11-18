import json
from pathlib import Path
import shutil
import subprocess
import unittest

from xdownloader.browser_resolver import capture_script


@unittest.skipUnless(shutil.which('node'), 'JS 观察器开发测试需要 Node.js 18+；应用运行不需要')
class BrowserScriptTests(unittest.TestCase):
    def run_case(self, name):
        result = subprocess.run(
            [shutil.which('node'), str(Path(__file__).with_name('capture_requests.cjs')), name],
            input=json.dumps(capture_script('123456789')), text=True,
            capture_output=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_get_query_and_url_object(self): self.run_case('get')
    def test_post_json_form_and_blob(self): self.run_case('post')
    def test_request_body_clone_and_override(self): self.run_case('request')
    def test_xhr_post_and_reuse(self): self.run_case('xhr')
    def test_other_target_origin_and_malformed_body(self): self.run_case('isolation')
    def test_response_limits(self): self.run_case('responses')
