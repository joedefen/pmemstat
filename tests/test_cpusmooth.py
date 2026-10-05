"""Unit tests for :mod:`pmemstat.CpuSmooth`.

These are Linux-only (they read ``/proc``) and focus on the deterministic
pieces: the ANSI escape helpers and the basic behaviour of the CPU sampler
when pointformatted at the current process.
"""
import os
import unittest

from pmemstat.CpuSmooth import Term, SysStat, CpuSmooth


class TestTerm(unittest.TestCase):
    """ANSI escape sequence helpers."""

    def test_escape_sequences(self):
        self.assertEqual(Term.erase_line(), '\x1B[2K')
        self.assertEqual(Term.erase_to_eol(), '\x1B[0K')
        self.assertEqual(Term.bold(), '\x1B[1m')
        self.assertEqual(Term.reverse_video(), '\x1B[7m')
        self.assertEqual(Term.normal_video(), '\x1B[m')
        self.assertEqual(Term.col(5), '\x1B[5G')
        self.assertEqual(Term.clear_screen(), '\x1B[H\x1B[2J\x1B[3J')

    def test_positional_helpers(self):
        self.assertEqual(Term.pos_up(3), '\x1B[3F')
        self.assertEqual(Term.pos_down(2), '\x1B[2E')
        self.assertEqual(Term.pos_up(0), '')
        self.assertEqual(Term.pos_down(0), '')


class TestSysStat(unittest.TestCase):
    """The system-wide CPU tick sampler is a singleton."""

    def test_singleton_identity(self):
        self.assertIs(SysStat.get_singleton(), SysStat.get_singleton())

    def test_refresh_returns_delta(self):
        delta = SysStat.refresh()
        self.assertGreaterEqual(delta.cpu_cnt, 1)
        self.assertGreaterEqual(delta.percent, 0)


class TestCpuSmooth(unittest.TestCase):
    """The per-process CPU sampler against this very process."""

    def test_nickname_of_self_is_nonempty(self):
        cpu = CpuSmooth(os.getpid())
        nickname = cpu.get_nickname()
        self.assertIsInstance(nickname, str)
        self.assertTrue(nickname)

    def test_refresh_cpu_needs_two_samples(self):
        cpu = CpuSmooth(os.getpid())
        self.assertEqual(cpu.refresh_cpu(), 0)  # a single sample cannot rate
        value = cpu.refresh_cpu()
        self.assertIsInstance(value, (int, float))
        self.assertGreaterEqual(value, 0)


if __name__ == '__main__':
    unittest.main()
