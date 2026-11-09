
import unittest

from core.system_info import SystemInfo


class SystemInfoTests(unittest.TestCase):

    def setUp(self):
        self.info = SystemInfo()

    def test_memory(self):
        result = self.info.memory()
        self.assertIn("total", result)
        self.assertIn("used", result)
        self.assertIn("percent", result)

    def test_cpu(self):
        result = self.info.cpu()
        self.assertIn("percent", result)
        self.assertIn("cores", result)

    def test_disk(self):
        result = self.info.disk("/")
        self.assertIn("total", result)
        self.assertIn("free", result)

    def test_snapshot(self):
        result = self.info.snapshot()
        self.assertIn("cpu", result)
        self.assertIn("memory", result)
        self.assertIn("disk", result)
        self.assertIn("uptime_seconds", result)

    def test_invalid_disk_path(self):
        with self.assertRaises(Exception):
            self.info.disk("")


if __name__ == "__main__":
    unittest.main()
