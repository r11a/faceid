import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from app.main import parse_camera_graph
from app.scenarios import ScenarioManager
from app.reid import AppearanceReID
from app import main as main_module
from app.alerting import AlertStore


class StartupConfigTests(unittest.TestCase):
    def test_alerts_are_ready_before_workers_and_routes_reuse_the_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'config.yaml').write_text(yaml.safe_dump({
                'frigate': {'url': 'http://frigate.invalid:5000'},
                'mqtt': {'host': 'mqtt.invalid'},
                'faceid': {'liveness_enabled': False},
            }), encoding='utf-8')
            started = []
            original_build = main_module.build_app

            def verify_start(processor):
                self.assertIsInstance(getattr(processor, 'alerts', None), AlertStore)
                self.assertIs(processor.alerts.processor.camera_profiles, processor.camera_profiles)
                started.append(processor.alerts)

            def verify_build(*args):
                app = original_build(*args)
                self.assertIs(args[3].alerts, started[0])
                return app

            with patch.object(main_module, 'BASE', root), \
                 patch.object(main_module, 'FaceEngine', return_value=SimpleNamespace(faces=lambda image: [])), \
                 patch.object(main_module.EventProcessor, 'start', autospec=True, side_effect=verify_start), \
                 patch.object(main_module, 'build_app', side_effect=verify_build), \
                 patch.object(main_module, 'start_auto_backup'), \
                 patch.object(main_module.uvicorn, 'run'):
                main_module.main()
            self.assertEqual(len(started), 1)

    def test_complete_startup_with_home_assistant_610_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'config.yaml').write_text(yaml.safe_dump({
                'frigate':{'url':'http://frigate.invalid:5000'},
                'mqtt':{'host':'mqtt.invalid'},
                'faceid':{'camera_graph':'{}','liveness_enabled':False},
            }),encoding='utf-8')
            with patch.object(main_module,'BASE',root), \
                 patch.object(main_module,'FaceEngine',return_value=SimpleNamespace(faces=lambda image:[])), \
                 patch.object(main_module.EventProcessor,'start'), \
                 patch.object(main_module,'start_auto_backup'), \
                 patch.object(main_module.uvicorn,'run') as serve:
                main_module.main()
            self.assertTrue(serve.called)
            app=serve.call_args.args[0]
            self.assertTrue(any(route.path=='/api/faces/search' for route in app.routes))

    def test_610_json_string_is_compatible_with_both_camera_services(self):
        for value in ['{}', '{"door":["hall"],"hall":["door"]}',
                      {'door':['hall']}, None, '']:
            with self.subTest(value=value):
                graph=parse_camera_graph(value)
                scenarios=ScenarioManager(None,camera_graph=graph)
                reid=AppearanceReID(camera_graph=graph)
                self.assertIsInstance(scenarios.camera_graph,dict)
                self.assertEqual(scenarios.camera_graph,reid.camera_graph)

    def test_invalid_graph_has_a_specific_configuration_error(self):
        for value in ['[]','invalid',{'door':'hall'}, {'door':[3]}]:
            with self.subTest(value=value),self.assertRaisesRegex(ValueError,'camera_graph'):
                parse_camera_graph(value)

    @unittest.skipUnless(shutil.which('jq'),'jq is required to execute the add-on configuration encoder')
    def test_actual_addon_encoder_produces_a_mapping_and_safe_yaml(self):
        script=(Path(__file__).parents[1]/'faceid-addon/run.sh').read_text(encoding='utf-8')
        expression=re.search(r"^  camera_graph: \$\(cfg_json '([^']+)'\)",script,re.M).group(1)
        for graph in ['{}','{"door":["hall"]}', {'door':['hall']}]:
            encoded=subprocess.run(['jq','-c',expression],input=json.dumps({'camera_graph_json':graph}),
                text=True,capture_output=True,check=True).stdout
            parsed=yaml.safe_load('faceid:\n  camera_graph: '+encoded)['faceid']['camera_graph']
            self.assertIsInstance(parsed,dict)
            ScenarioManager(None,camera_graph=parsed)
            AppearanceReID(camera_graph=parsed)


if __name__=='__main__': unittest.main()
