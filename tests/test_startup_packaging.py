import unittest
from pathlib import Path
class StartupPackagingTests(unittest.TestCase):
 def test_startup_script_has_linux_shebang_and_no_carriage_returns(self):
  root=Path(__file__).resolve().parents[1]
  data=(root/'faceid-addon/run.sh').read_bytes()
  self.assertTrue(data.startswith(b'#!/usr/bin/with-contenv bashio\n'))
  self.assertNotIn(b'\r',data)
 def test_docker_build_normalizes_and_checks_shell_script(self):
  root=Path(__file__).resolve().parents[1]
  text=(root/'faceid-addon/Dockerfile').read_text()
  self.assertIn("sed -i 's/\\r$//' /run.sh",text)
  self.assertIn('bash -n /run.sh',text)
