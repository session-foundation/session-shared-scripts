import os
import unittest

from session_ops.ops import registry

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestDocs(unittest.TestCase):
    def test_every_registered_job_has_its_doc(self):
        for job in registry.load():
            with self.subTest(job=job.name):
                self.assertTrue(os.path.exists(
                    os.path.join(ROOT, "docs", "jobs", f"{job.name}.md")))
