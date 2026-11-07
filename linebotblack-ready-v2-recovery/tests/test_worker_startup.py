import ast
import importlib.util
import runpy
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('baseline', Path(__file__).with_name('test_reliability.py'))
baseline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(baseline)
bot = baseline.bot


class WorkerStartupTests(unittest.TestCase):
    def tearDown(self):
        bot._reset_storage_process()

    def test_import_does_not_launch_bootstrap(self):
        tree = ast.parse(Path(bot.__file__).read_text(encoding='utf-8'))
        calls = [n.value.func.id for n in tree.body
                 if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                 and isinstance(n.value.func, ast.Name)]
        self.assertNotIn('_start_storage_init', calls)

    def test_child_discards_inherited_initializing_flag_and_locks(self):
        bot._storage_state.update(ready=True, initializing=True)
        bot._storage_pid = -1
        old_lock = bot._storage_lock
        bot._ensure_storage_process()
        self.assertFalse(bot._storage_state['ready'])
        self.assertFalse(bot._storage_state['initializing'])
        self.assertIsNot(old_lock, bot._storage_lock)
        with patch.object(bot.threading, 'Thread') as thread:
            bot._start_storage_init()
            self.assertEqual(thread.call_count, 1)

    def test_worker_hook_starts_initialization(self):
        config = runpy.run_path(str(Path(bot.__file__).with_name('gunicorn.conf.py')))
        self.assertFalse(config['preload_app'])
        with patch.object(bot, '_start_storage_init') as start:
            config['post_worker_init'](None)
            start.assert_called_once()

    def test_readiness_exposes_progress(self):
        bot._reset_storage_process()
        bot._storage_state.update(initializing=True, operation='READ test', started_at=bot.time.monotonic())
        response = bot.app.test_client().get('/ready')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json['operation'], 'READ test')
        self.assertEqual(response.json['pid'], bot.os.getpid())
        self.assertIn('elapsed_seconds', response.json)


if __name__ == '__main__':
    unittest.main(verbosity=2)
