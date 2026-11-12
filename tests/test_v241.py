"""Regression coverage for recovery and the local volume notification."""
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from core import watchdog
from player import StallDetector, VolumeWakeup, XrunRecovery


class RecoveryTests(unittest.TestCase):
    def test_watchdog_is_inert_without_systemd_socket(self):
        with patch.dict(os.environ, {}, clear=True), patch('core.socket.socket') as sock:
            watchdog()
        sock.assert_not_called()

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Live Unix socket test runs on the NAS')
    def test_watchdog_and_volume_use_local_datagrams(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as tmp:
            path = str(Path(tmp) / 'volume.sock')
            wake = VolumeWakeup(path)
            try:
                with patch.dict(os.environ, {'NOTIFY_SOCKET':path}):
                    watchdog()
                self.assertEqual(wake.sock.recv(64), b'WATCHDOG=1')
                with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sender:
                    sender.sendto(b'volume', path)
                self.assertEqual(wake.sock.recv(64), b'volume')
            finally:
                wake.close()
            self.assertFalse(Path(path).exists())

    def test_stall_requires_the_same_owned_running_pcm(self):
        detector = StallDetector()
        pcm = dict(owner_pid='5', state='RUNNING', hw_ptr='200')
        with patch('player.os.path.isdir', return_value=True):
            self.assertFalse(detector.observe('analog', 10, pcm))
            self.assertTrue(detector.observe('analog', 10, pcm))
            self.assertFalse(detector.observe('analog', 10, dict(pcm, state='PAUSED')))
            self.assertFalse(detector.observe('analog', 10, pcm))
            self.assertFalse(detector.observe('analog', 11, pcm))
        recovery = XrunRecovery()
        self.assertFalse(recovery.observe(0, True, False))
        self.assertFalse(recovery.observe(30, True, False))
        self.assertFalse(recovery.observe(31, True, True))
        self.assertTrue(recovery.observe(47, True, True))
        self.assertFalse(recovery.observe(64, True, True))
