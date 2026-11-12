"""Simulation tests: no NAS, Bluetooth services or ALSA hardware are touched."""
import ctypes
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

SRC=Path(__file__).resolve().parents[1]/'src'
sys.path.insert(0,str(SRC))
import volume
import bluebridge_dashboard as dash


class VolumeTests(unittest.TestCase):
    def test_defaults_preserve_existing_output_and_attenuate_new_pro(self):
        self.assertEqual(volume.default_level('integrated'),100)
        self.assertEqual(volume.default_level('scarlett'),100)
        self.assertEqual(volume.default_level('studio'),70)

    def test_reject_bad_level(self):
        for n in (True,False,-1,101,12.5,'100',None):
            with self.assertRaises(ValueError):
                volume.validate_level('integrated',n,{'integrated':'X'})
        with self.assertRaises(ValueError):
            volume.validate_level('evil;rm',20,{'integrated':'X'})

    def test_levels_read_and_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'levels.json'
            path.write_text(json.dumps({'version':1,'levels':{'scarlett':81,'integrated':93,'studio':25,'bogus':100}}))
            got=volume.load_levels(path,{'scarlett':'','integrated':'','studio':''})
            self.assertEqual(got,{'scarlett':81,'integrated':93,'studio':25})

    def test_corrupt_prefs_fail_closed(self):
        with self.assertRaises(ValueError):
            volume.levels_from({'version':1,'levels':{'integrated':'100'}},{'integrated':'Card'})
        with tempfile.TemporaryDirectory() as temp:
            missing=Path(temp)/'missing.json'
            with self.assertRaises(RuntimeError):
                volume.load_levels(missing, {'scarlett':'Scarlett'})

    def test_per_card_setting_independent(self):
        allowed={'scarlett':'Scarlett Solo','integrated':'Realtek','studio':'New'}
        got=volume.levels_from({'version':1,'levels':{'scarlett':80,'integrated':95,'studio':70}},allowed)
        self.assertEqual(got['scarlett'],80)
        self.assertEqual(got['integrated'],95)
        self.assertEqual(got['studio'],70)

    def test_linear_attenuation_monotonic_and_zero_boost(self):
        self.assertEqual(volume.raw_level(0),0)
        self.assertEqual(volume.raw_level(100),255)
        self.assertTrue(0<volume.raw_level(10)<volume.raw_level(50)<255)
        # 50% linear amplitude => -6.02 dB => approximately 230/255.
        self.assertLess(abs(volume.raw_level(50)-229),3)

    def test_alsa_output_templating_rejects_injection(self):
        example={'card_id':'Generic','device':0,'card':1}
        data=volume.render_alsa_config(example)
        self.assertIn('type softvol',data)
        self.assertIn('control { name "BlueBridge Base Volume" card 1 }',data)
        self.assertIn('plughw:CARD=Generic,DEV=0',data)
        self.assertIn('max_dB 0.0',data)
        with self.assertRaises(ValueError):
            volume.render_alsa_config({**example,'card_id':'evil; touch /tmp/X'})
        with self.assertRaises(ValueError):
            volume.render_alsa_config({**example,'card':1.5})

    @unittest.skipUnless(sys.platform.startswith('linux'), 'libasound is a Linux dependency')
    def test_parses_through_real_libasound(self):
        # Syntax check only: no actual PCM or soundcard needs to be present.
        lib=ctypes.CDLL('libasound.so.2')
        lib.snd_input_stdio_open.argtypes=[ctypes.POINTER(ctypes.c_void_p),ctypes.c_char_p,ctypes.c_char_p]
        lib.snd_config_top.argtypes=[ctypes.POINTER(ctypes.c_void_p)]
        lib.snd_config_load.argtypes=[ctypes.c_void_p,ctypes.c_void_p]
        lib.snd_input_close.argtypes=[ctypes.c_void_p]
        lib.snd_config_delete.argtypes=[ctypes.c_void_p]
        with tempfile.TemporaryDirectory() as temp:
            file=Path(temp)/'pcm.conf'
            file.write_text(volume.render_alsa_config({'card_id':'Generic','device':0,'card':1}))
            stream=ctypes.c_void_p(); top=ctypes.c_void_p()
            self.assertEqual(lib.snd_input_stdio_open(ctypes.byref(stream),str(file).encode(),b'r'),0)
            self.assertEqual(lib.snd_config_top(ctypes.byref(top)),0)
            self.assertEqual(lib.snd_config_load(top,stream),0)
            lib.snd_input_close(stream); lib.snd_config_delete(top)

    def test_alsa_mixer_changes_do_not_restart_player_or_touch_master(self):
        read="numid=39,iface=MIXER,name='BlueBridge Base Volume'\n  ; type=INTEGER,access=rw---R--,values=2,min=0,max=255,step=0\n  : values=229,229\n"
        val=volume.raw_level(55)
        read_correct=read.replace('229,229',f'{val},{val}')
        class Result:
            def __init__(self, stdout):
                self.returncode=0;self.stdout=stdout;self.stderr=''
        with patch.object(volume,'_run',side_effect=[Result(read),Result(''),Result(read_correct)]) as execute:
            volume.apply_level(1,55)
        commands=[x.args[0] for x in execute.call_args_list]
        self.assertEqual(commands[0][:4],['/usr/bin/amixer','-c','1','cget'])
        self.assertEqual(commands[1][:4],['/usr/bin/amixer','-c','1','cset'])
        self.assertEqual(commands[1][-1],str(val))
        self.assertIn('BlueBridge Base Volume',commands[1][-2])
        self.assertNotIn('Master',commands[1])
        self.assertNotIn('systemctl',str(commands))

    def test_volume_prefs_for_future_usb(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'professional_usb.json'
            path.write_text(json.dumps({'professional_usb':[{'id':'nextgen','label':'USB Studio','priority':120}]}))
            self.assertIn('nextgen',volume.known_cards(path))


class DashboardTests(unittest.TestCase):
    def test_controls_for_two_cards_and_offline_registered(self):
        saved={'updated':100,'service':{'label':'Activo','state':'online'},
               'audio_devices':[{'id':'integrated','label':'Realtek','priority':-100,
                                  'connected':True,'selected':True,'ready':True,'state':'En uso'}]}
        pl={'updated':100,'output_id':'integrated','pid':313,'output_ready':True,
            'volume_supported':True,'volume_mode':'digital','volume_base_applied':100}
        def fixture(path,default=None):
            if path == dash.STATE: return saved
            if path == dash.PLAYER: return pl
            if path == dash.PREFS: return {'version':1,'levels':{'scarlett':70,'integrated':100}}
            return default
        with patch.object(dash,'_read_json',side_effect=fixture),patch.object(dash.time,'time',return_value=101):
            result=dash.status()
        ids=[x['id'] for x in result['audio_devices']]
        self.assertEqual(ids,['integrated','scarlett'])
        self.assertEqual([d['base_volume'] for d in result['audio_devices']],[100,70])
        self.assertEqual(result['audio_devices'][1]['state'],'No conectada')
        self.assertTrue(result['audio_devices'][0]['base_applied'])
        self.assertEqual(result['audio_devices'][0]['volume_control'],'available')
        self.assertEqual(result['audio_devices'][1]['volume_control'],'unverified')

    def test_unavailable_control_stays_grey_and_server_rejects(self):
        saved={'updated':100,'audio_devices':[{'id':'scarlett','label':'Scarlett',
            'priority':100,'connected':True,'selected':True,'ready':True}]}
        player={'updated':100,'output_id':'scarlett','pid':412,'output_ready':True,
                'volume_supported':False,'volume_mode':'bluetooth_only',
                'volume_error':'Mixer element not found'}
        def fixture(path, default=None):
            if path == dash.STATE:return saved
            if path == dash.PLAYER:return player
            if path == dash.PREFS:return {'version':1,'levels':{'scarlett':100}}
            return default
        with patch.object(dash,'_read_json',side_effect=fixture), \
             patch.object(dash.time,'time',return_value=101):
            result=dash.status()
            self.assertEqual(result['audio_devices'][0]['volume_control'],'unsupported')
            with patch.object(dash.os,'geteuid',return_value=0):
                with self.assertRaisesRegex(RuntimeError,'no verificado'):
                    dash.operate({'action':'set_volume','id':'scarlett','level':50})

    def test_volume_check_rejects_missing_control_even_after_pcm_open(self):
        output={'card_id':'Generic','device':0,'card':1}
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            class OK: returncode=0; stdout=''; stderr=''
            class Fail: returncode=1; stdout=''; stderr='Mixer element not found'
            with patch.object(volume,'_run',side_effect=[OK(),Fail()]):
                with self.assertRaisesRegex(RuntimeError,'Control digital ausente'):
                    volume.create_and_prime(output,root)

    def test_html_has_endpoint_csrf_and_no_pairing_button(self):
        html=dash.card('test-csrf')
        self.assertIn('type = \'range\'',html)
        self.assertIn('"test-csrf"',html)
        self.assertIn("action:'set_volume'",html)
        self.assertIn('/api/bluebridge',html)
        self.assertNotIn('Emparejar 120',html)
        self.assertNotIn('Cerrar emparejamiento',html)

    def test_csrf_is_escaped_against_script_breakout(self):
        html=dash.card('</script><script>alert(1)</script>')
        self.assertEqual(html.count('</script>'),1)
        self.assertIn('\\u003c/script\\u003e',html)

    def test_rejects_invalid_request_before_file_access(self):
        with self.assertRaises(ValueError):
            dash.operate({'action':'set_volume','id':'integrated','level':True})
        with self.assertRaises(ValueError):
            dash.operate({'action':'set_volume','id':'HDMI','level':100})
        with self.assertRaises(ValueError):
            dash.operate({'action':'set_volume','id':'integrated','level':70,'shell':'rm'})

    def test_dashboard_only_configured_cards(self):
        with patch.object(dash,'_read_json',return_value={'professional_usb':[{'id':'studio','label':'Studio','priority':110}]}):
            allowed=dash._allowed()
        self.assertIn('studio',allowed)
        self.assertNotIn('HDMI',allowed)
        self.assertNotIn('DP',allowed)


if __name__=='__main__':
    unittest.main()
