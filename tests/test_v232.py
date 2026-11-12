"""Regression tests matching observed NAS ALSA cget output, fallback and UI."""
from pathlib import Path
import sys, unittest
from unittest.mock import patch
SRC=Path(__file__).resolve().parents[1]/'src'
sys.path.insert(0,str(SRC))
import volume
import bluebridge_dashboard as dash

RAW = ("numid=39,iface=MIXER,name='BlueBridge Base Volume'\n"
       "  ; type=INTEGER,access=rw---R--,values=2,min=0,max=255,step=0\n"
       "  : values=255,255\n"
       "  | dBscale-min=-60.00dB,step=0.25dB,mute=0\n")

class Result:
    def __init__(self, out, rc=0):
        self.returncode=rc
        self.stdout=out if rc==0 else ''
        self.stderr=out if rc else ''

class V232(unittest.TestCase):
    def test_cget_not_sget_raw_control(self):
        with patch.object(volume,'_run',return_value=Result(RAW)) as m:
            self.assertEqual(volume.read_raw_control(1),[255,255])
        self.assertEqual(m.call_args.args[0], ['/usr/bin/amixer','-c','1','cget',volume.RAW_CONTROL])
    def test_reject_mismatched_raw_control(self):
        cases=(RAW.replace('max=255','max=64'), RAW.replace('iface=MIXER','iface=CARD'),
               RAW.replace('rw---','r----'), RAW.replace('values=2','values=1'))
        for case in cases:
            with self.subTest(case=case):
                with patch.object(volume,'_run',return_value=Result(case)):
                    with self.assertRaises(RuntimeError):volume.read_raw_control(1)
    def test_apply_level_confirms_readback(self):
        raw=volume.raw_level(50)
        updated=RAW.replace('255,255',f'{raw},{raw}')
        with patch.object(volume,'_run',side_effect=[Result(RAW),Result(''),Result(updated)]) as m:
            self.assertTrue(volume.apply_level(1,50))
        self.assertEqual(m.call_args_list[1].args[0], ['/usr/bin/amixer','-c','1','cset',volume.RAW_CONTROL,str(raw)])
    def test_apply_level_mismatch_does_not_claim_success(self):
        with patch.object(volume,'_run',side_effect=[Result(RAW),Result(''),Result(RAW)]):
            with self.assertRaisesRegex(RuntimeError,'no confirmó'):
                volume.apply_level(1,50)
    def test_create_and_prime_checks_raw_control(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp:
            output={'card_id':'Generic','device':0,'card':1}
            with patch.object(volume,'_run',side_effect=[Result(''),Result(RAW)]) as m:
                _,env=volume.create_and_prime(output, Path(tmp))
            self.assertIn('ALSA_CONFIG_PATH',env)
            self.assertEqual(m.call_args_list[1].args[0][3],'cget')
    def test_dashboard_controller_omits_connected_ready_but_selected_is_ready(self):
        saved={'updated':100,'audio_devices':[{'id':'integrated','label':'Realtek',
                  'priority':-100,'selected':True,'in_use':True,'state':'En uso'}]}
        playing={'updated':100,'output_id':'integrated','pid':456,'output_ready':True,
                 'volume_supported':True,'volume_mode':'digital','volume_base_applied':100}
        def fixture(p,default=None):
            if p==dash.STATE:return saved
            if p==dash.PLAYER:return playing
            if p==dash.PREFS:return {'version':1,'levels':{'integrated':100,'scarlett':100}}
            return default
        with patch.object(dash,'_read_json',side_effect=fixture),patch.object(dash.time,'time',return_value=101):
            present=dash.status()['audio_devices']
        self.assertTrue(present[0]['connected'])
        self.assertTrue(present[0]['ready'])
        self.assertEqual(present[0]['volume_control'],'available')
        self.assertEqual(present[1]['volume_control'],'unverified')
    def test_no_change_to_core_selection(self):
        import hashlib
        # SHA256 original de audio_select.py en v2.3.1.
        esperado = '38708b6190c1e0d25b7b7c0f98cfc5a9942359434c91f27f3de309382d7d9237'
        actual = hashlib.sha256(
            (SRC / 'audio_select.py').read_bytes()
        ).hexdigest()
        self.assertEqual(actual, esperado)
    def test_fallback_uses_v22_playback_options(self):
        player=(SRC/'player.py').read_text()
        self.assertIn("if volume_supported is True:\n                        options.append('--volume=software')",player)
        self.assertIn("options += ['-D', output_pcm, '--single-audio']",player)

if __name__ == '__main__': unittest.main()
