"""Exercise the Windows locking branch without requiring Windows locally."""

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.services.portfolio_state import PortfolioStateStore


class WindowsLockTests(unittest.TestCase):
    def test_windows_locks_one_byte_and_releases_on_close(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PortfolioStateStore('sqlite:///' + str(Path(directory) / 'state.db'))
            windows = types.SimpleNamespace(LK_NBLCK=2, locking=Mock())
            try:
                with patch('app.services.portfolio_state.sys.platform', 'win32'):
                    with patch.dict(sys.modules, {'msvcrt': windows}):
                        with store.cycle_lock():
                            self.assertTrue(store._locked)
                            windows.locking.assert_called_once()
                            self.assertEqual(windows.locking.call_args.args[1:], (2, 1))
                self.assertFalse(store._locked)
                lock_files = list(Path(directory).glob('*.lock'))
                self.assertEqual(len(lock_files), 1)
                self.assertEqual(lock_files[0].read_text(), '0')
            finally:
                store.engine.dispose()

    def test_windows_lock_contention_fails_without_entering_cycle(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PortfolioStateStore('sqlite:///' + str(Path(directory) / 'state.db'))
            windows = types.SimpleNamespace(LK_NBLCK=2, locking=Mock(side_effect=OSError('locked')))
            try:
                with patch('app.services.portfolio_state.sys.platform', 'win32'):
                    with patch.dict(sys.modules, {'msvcrt': windows}):
                        with self.assertRaisesRegex(RuntimeError, 'Another cycle'):
                            with store.cycle_lock():
                                self.fail('A contending writer entered the cycle')
                self.assertFalse(store._locked)
            finally:
                store.engine.dispose()


if __name__ == '__main__':
    unittest.main()
