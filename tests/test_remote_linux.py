import json
import os
from pathlib import Path
import tempfile
import time
import subprocess
import shutil
import threading
import unittest
from unittest.mock import patch
from automation import remote
from automation.common import TaskError
from automation.common import atomic_json
from automation.fixtures import fixture_server


@unittest.skipUnless(os.name == "posix" and Path("/proc").exists(), "Linux /proc and flock required")
class LinuxJobTests(unittest.TestCase):
    def test_interrupted_task_cannot_silently_change_release(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(remote, 'ROOT', Path(directory)):
                task = remote.jobdir('frozen')
                task.mkdir(parents=True)
                atomic_json(task / 'status.json', {'task_id': 'frozen', 'state': 'interrupted'})
                atomic_json(task / 'identity.json', {'release': '/old-release', 'profile': 'basic'})
                with self.assertRaisesRegex(TaskError, 'release_conflict'):
                    remote.start('frozen', 30, Path('/new-release'))

    def test_configurable_collection_profile_is_not_an_acceptance_fixture(self):
        original = Path(__file__).resolve().parents[1]
        server = fixture_server(original / 'fixtures', port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / 'state'
                release = Path(directory) / 'release'
                shutil.copytree(original / 'automation', release / 'automation', ignore=shutil.ignore_patterns('__pycache__'))
                (release / 'config').mkdir()
                sources = [{'url': f'http://127.0.0.1:{server.server_port}/data.json',
                            'automation_allowed': True, 'permission_basis': 'owned fixture'}]
                atomic_json(release / 'config/collection-sources.json', sources)
                with patch.object(remote, 'ROOT', root), patch.dict(os.environ, {'AUTOMATION_DATA': str(root)}):
                    remote.start('custom-collection', 30, release, profile='collection')
                    for _ in range(150):
                        state = remote.status('custom-collection')
                        if state['state'] in {'completed', 'failed'} and not remote.busy():
                            break
                        time.sleep(0.1)
                    self.assertEqual(state['state'], 'completed')
                    self.assertEqual(state['completed'], 1)
                    self.assertFalse((root / 'jobs/custom-collection/acceptance-report.json').exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_snapshot_excludes_only_own_exec_group(self):
        own = subprocess.Popen(['sleep', '30'])
        foreign = subprocess.Popen(['sleep', '30'], start_new_session=True)
        try:
            snapshot = remote.process_snapshot()
            self.assertNotIn(str(own.pid), snapshot)
            self.assertIn(str(foreign.pid), snapshot)
            self.assertEqual(os.getpgid(own.pid), os.getpgrp())
            self.assertNotEqual(os.getpgid(foreign.pid), os.getpgrp())
        finally:
            own.terminate()
            foreign.terminate()
            own.wait(timeout=5)
            foreign.wait(timeout=5)

    def test_real_detached_job_lock_result_and_idempotency(self):
        release = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(remote, "ROOT", root), patch.dict(os.environ, {"AUTOMATION_DATA": str(root)}):
                first = remote.start("linux-smoke", 30, release)
                self.assertEqual(first["job_id"], "linux-smoke")
                self.assertTrue(remote.busy())
                with self.assertRaisesRegex(TaskError, "machine_busy"):
                    remote.start("other", 30, release)
                for _ in range(100):
                    state = remote.status("linux-smoke")
                    if state["state"] in {"completed", "failed"} and not remote.busy():
                        break
                    time.sleep(0.1)
                self.assertEqual(state["state"], "completed")
                self.assertEqual(state["completed"], 2)
                package = remote.package_result("linux-smoke")
                self.assertTrue(Path(package["path"]).exists())
                again = remote.start("linux-smoke", 30, release)
                self.assertEqual(again["state"], "completed")
                manifest = json.loads((root / "jobs/linux-smoke/results.manifest.json").read_text())
                self.assertEqual(manifest["count"], 2)
