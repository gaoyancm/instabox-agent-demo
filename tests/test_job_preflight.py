from pathlib import Path
import tempfile
import unittest
from automation.common import TaskError, atomic_json
from automation.jobs import JobRunner


class JobPreflightTests(unittest.TestCase):
    def test_empty_collection_is_rejected_before_machine_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            atomic_json(root / 'vendor/manifest.json', {})
            atomic_json(root / 'config/collection-sources.json', [])
            with self.assertRaisesRegex(TaskError, 'no_collection_sources_configured'):
                JobRunner(None, 'not-started', root, profile='collection')
