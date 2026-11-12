import tempfile
import unittest

from test_v22 import FakeHardware, discover_outputs
from player import ScarlettFallback


class ScarlettFallbackTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.hw = FakeHardware(temp.name)
        self.hw.card(0, 'HDMI', 'HDMI 0', device=3)
        self.hw.card(1, 'Generic', 'ALC887-VD Analog')
        self.policy = ScarlettFallback()

    def scan(self, seconds):
        preferred, candidates = discover_outputs(
            dict(scarlett=dict(vendor='1235', product='8211', serial='FOC123')),
            sysroot=self.hw.sys, devroot=self.hw.dev, procroot=self.hw.proc)
        return self.policy.choose(preferred, candidates, seconds)

    def connect_scarlett(self, ready=False):
        self.hw.usb('1-2', '1235', '8211', 'FOC123')
        if ready:
            self.hw.card(2, 'BlueBridge', 'USB Audio', usb='1-2')

    def test_ready_scarlett_is_preferred_immediately_on_start(self):
        self.connect_scarlett(ready=True)
        self.assertEqual(self.scan(0)['id'], 'scarlett')

    def test_usb_present_without_alsa_has_bounded_grace(self):
        self.connect_scarlett()
        self.assertFalse(self.scan(0)['ready'])
        self.assertEqual(self.scan(5)['id'], 'scarlett')
        self.assertEqual(self.scan(6)['id'], 'integrated')
        self.assertEqual(self.scan(3600)['id'], 'integrated')

    def test_absent_scarlett_and_late_return(self):
        self.assertEqual(self.scan(0)['id'], 'integrated')
        self.assertEqual(self.scan(3600)['id'], 'integrated')
        self.connect_scarlett()
        self.assertEqual(self.scan(3602)['id'], 'integrated')
        self.hw.card(2, 'BlueBridge', 'USB Audio', usb='1-2')
        self.assertEqual(self.scan(3604)['id'], 'integrated')
        self.assertEqual(self.scan(3608)['id'], 'scarlett')

    def test_flapping_alsa_does_not_interrupt_realtek(self):
        self.connect_scarlett()
        self.scan(0)
        self.assertEqual(self.scan(6)['id'], 'integrated')
        self.hw.card(2, 'BlueBridge', 'USB Audio', usb='1-2')
        self.assertEqual(self.scan(8)['id'], 'integrated')
        pcm = self.hw.dev/'snd/pcmC2D0p'
        pcm.unlink()
        self.assertEqual(self.scan(10)['id'], 'integrated')
        pcm.touch()
        self.assertEqual(self.scan(12)['id'], 'integrated')
        self.assertEqual(self.scan(14)['id'], 'integrated')
        self.assertEqual(self.scan(16)['id'], 'scarlett')

    def test_late_alsa_within_grace_skips_realtek_if_stable_in_time(self):
        self.connect_scarlett()
        self.scan(0)
        self.hw.card(2, 'BlueBridge', 'USB Audio', usb='1-2')
        self.assertFalse(self.scan(2)['ready'])
        self.assertEqual(self.scan(6)['id'], 'scarlett')

    def test_no_fallback_to_hdmi_if_realtek_missing(self):
        self.connect_scarlett()
        (self.hw.sys/'class/sound/card1').unlink()
        self.assertFalse(self.scan(0)['ready'])
        self.assertEqual(self.scan(3600)['id'], 'scarlett')
        self.assertFalse(self.scan(3600)['ready'])
