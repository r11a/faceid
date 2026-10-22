import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import threading
import shutil
from unittest.mock import Mock, patch
from app import access1_setup as setup


class SetupTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("openssl"),"OpenSSL required")
    def test_prepare_certificate_no_overwrite_no_credentials_no_option_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            integration=SimpleNamespace(path=Path(directory)/"access1.json",_lock=threading.RLock(),
                                        options={"enabled":False})
            with patch.object(setup,"addon_hostname",return_value="local-faceid"), \
                    patch.object(setup.os, 'chmod', wraps=os.chmod) as chmod:
                first=setup.prepare_certificate(integration)
                key=Path(first["tls_key"]).read_bytes()
                second=setup.prepare_certificate(integration)
            self.assertEqual(first,second)
            self.assertEqual(Path(first["tls_key"]).read_bytes(),key)
            self.assertEqual(integration.options,{"enabled":False})
            self.assertNotIn("PRIVATE KEY",json.dumps(first))
            self.assertEqual(set(Path(directory).iterdir()),{Path(directory)/"access1-wizard-tls"})
            self.assertTrue(any(call.args[0].name == 'directory-key.pem'
                                and call.args[1] == 0o600 for call in chmod.call_args_list))
            if os.name == 'posix':
                self.assertEqual(Path(first["tls_key"]).stat().st_mode & 0o777,0o600)
            # A broken existing setup must not be overwritten.
            Path(first["tls_cert"]).write_text("broken")
            with patch.object(setup,"addon_hostname",return_value="local-faceid"):
                with self.assertRaises(setup.HTTPException):setup.prepare_certificate(integration)
            self.assertEqual(Path(first["tls_cert"]).read_text(),"broken")
            self.assertEqual(Path(first["tls_key"]).read_bytes(),key)
    def test_only_public_certificate_and_no_tokens_or_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"public.pem"
            public = "-----BEGIN CERTIFICATE-----\nYWJj\n-----END CERTIFICATE-----"
            for contents, expected in (
                (public, public+"\n"),
                ("-----BEGIN PRIVATE KEY-----\nSECRET\n-----END PRIVATE KEY-----", ""),
                (public+"\n-----BEGIN PRIVATE KEY-----\nSECRET\n-----END PRIVATE KEY-----", ""),
                ("SECRET", ""),
            ):
                path.write_text(contents)
                self.assertEqual(setup.public_certificate(path),expected)
                self.assertNotIn("SECRET",setup.public_certificate(path))

    def test_supervisor_only_returns_hostname_not_options(self):
        response = Mock(status=200)
        response.read.return_value = json.dumps(
            {"data": {"hostname": "local-faceid", "options": {"secret": "PRIVATE"}}}).encode()
        connection = Mock()
        connection.getresponse.return_value = response
        with patch.dict(setup.os.environ, {"SUPERVISOR_TOKEN": "fixture"}), \
                patch.object(setup.http.client, "HTTPConnection", return_value=connection) as get:
            self.assertEqual(setup.addon_hostname(), "local-faceid")
        self.assertEqual(get.call_args.args[0], "supervisor")
        self.assertEqual(connection.request.call_args.args, ("GET", "/addons/self/info"))
        connection.close.assert_called_once()
        response.read.assert_called_once_with(131073)

    def test_discovery_failure_returns_no_configuration(self):
        with patch.dict(setup.os.environ, {}, clear=True), patch.object(setup.http.client, "HTTPConnection") as get:
            self.assertEqual(setup.addon_hostname(), "")
            get.assert_not_called()
        connection = Mock()
        connection.request.side_effect = OSError("private")
        with patch.dict(setup.os.environ, {"SUPERVISOR_TOKEN": "fixture"}), \
                patch.object(setup.http.client, "HTTPConnection", return_value=connection):
            self.assertEqual(setup.addon_hostname(), "")
