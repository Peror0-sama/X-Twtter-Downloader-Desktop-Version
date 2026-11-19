import subprocess,sys,unittest
from pathlib import Path
class WindowedArgumentTests(unittest.TestCase):
    def test_help_without_console(self):
        code="import sys,main; sys.stdout=sys.stderr=None; sys.argv=['XDownloader','--help']; main.main()"
        result=subprocess.run([sys.executable,'-c',code],cwd=Path(__file__).resolve().parents[1],capture_output=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
