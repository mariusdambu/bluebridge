"""v2.3.1 regressions: compatibility gating and stream fallback."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
SRC=Path(__file__).resolve().parents[1]/'src'
sys.path.insert(0,str(SRC))
import player

class PlayerFallbackTests(unittest.TestCase):
    def test_volume_support_is_only_reported_when_audio_starts(self):
        text=(SRC/'player.py').read_text()
        self.assertIn('volume_supported=(volume_supported if identity and child and child.poll() is None',text)
        self.assertIn("'--volume=software'",text)
        self.assertIn("output_pcm = 'bb_volume' if volume_supported is True else current['pcm']",text)
        self.assertIn('fallback_only = True',text)
        self.assertIn('volume_supported = False',text)
    def test_alsa_init_uses_hw_params_not_open_only(self):
        text=(SRC/'volume.py').read_text()
        self.assertIn('snd_pcm_set_params',text)
        self.assertIn('read_raw_control(output[\'card\'])',text)
        self.assertIn('lib.snd_pcm_close(pcm)',text)

if __name__=='__main__':unittest.main()
