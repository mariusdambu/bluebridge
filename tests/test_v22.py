import ast
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1] / 'src'
sys.path.insert(0, str(ROOT))
from audio_select import select_output, discover_outputs, dashboard_candidates, _profiles
from core import pcm_status
from player import XrunRecovery


class FakeHardware:
    def __init__(self, root):
        self.base = pathlib.Path(root)
        self.sys = self.base/'sys'; self.dev = self.base/'dev'; self.proc = self.base/'proc'
        for r in (self.sys/'class/sound', self.sys/'bus/usb/devices', self.dev/'snd', self.proc/'asound'):
            r.mkdir(parents=True,exist_ok=True)

    def card(self, number, cardid, pcmname, device=0, usb=None):
        if usb:
            where = self.sys/'devices/pci0000:00/usb1'/usb/(usb+':1.0')/'sound'/f'card{number}'
        else:
            where = self.sys/'devices/pci0000:00/0000:00:14.2'/'sound'/f'card{number}'
        where.mkdir(parents=True, exist_ok=True)
        (where/'id').write_text(cardid)
        link = self.sys/'class/sound'/f'card{number}'
        if not link.exists():
            link.symlink_to(where, target_is_directory=True)
        (self.dev/'snd'/f'pcmC{number}D{device}p').touch()
        info = self.proc/'asound'/f'card{number}'/f'pcm{device}p'/'info'
        info.parent.mkdir(parents=True,exist_ok=True)
        info.write_text(f'name: {pcmname}\nid: abc\n')
        return where

    def usb(self, name, vendor, product, serial, devnum='4'):
        where = self.sys/'devices/pci0000:00/usb1'/name
        where.mkdir(parents=True, exist_ok=True)
        for k,v in dict(idVendor=vendor,idProduct=product,serial=serial,devnum=devnum).items():
            (where/k).write_text(v)
        (self.sys/'bus/usb/devices'/name).symlink_to(where,target_is_directory=True)
        return where

    def choose(self, others=None):
        return select_output(dict(scarlett=dict(vendor='1235', product='8211', serial='FOC123')),
                             others, sysroot=self.sys,devroot=self.dev,procroot=self.proc)


class SelectorTests(unittest.TestCase):
    def setUp(self):
        self.ctx=tempfile.TemporaryDirectory()
        self.addCleanup(self.ctx.cleanup)
        self.hw=FakeHardware(self.ctx.name)
        self.hw.card(0,'HDMI','HDMI 0 [SAMSUNG]',device=3)
        self.hw.card(1,'Generic','ALC887-VD Analog')

    def test_fallback_is_integrated_analog_only(self):
        o=self.hw.choose()
        self.assertEqual((o['id'],o['card'],o['device']),('integrated',1,0))
        self.assertEqual(o['pcm'],'plughw:CARD=Generic,DEV=0')

    def test_scarlett_preferred_by_real_usb_identity(self):
        self.hw.usb('1-2','1235','8211','FOC123')
        self.hw.card(2,'BlueBridge','USB Audio', usb='1-2')
        o=self.hw.choose()
        self.assertEqual(o['id'],'scarlett')
        self.assertTrue(o['ready'])
        self.assertEqual(o['pcm'],'plughw:CARD=BlueBridge,DEV=0')

    def test_scarlett_physically_present_does_not_fall_back_during_driver_delay(self):
        self.hw.usb('1-2','1235','8211','FOC123')
        o=self.hw.choose()
        self.assertEqual(o['id'],'scarlett')
        self.assertTrue(o['connected'])
        self.assertFalse(o['ready'])
        self.assertIsNone(o['pcm'])

    def test_reattached_scarlett_with_new_card_index(self):
        self.hw.usb('1-2','1235','8211','FOC123')
        self.hw.card(7,'BlueBridge','USB Audio',usb='1-2')
        self.assertEqual(self.hw.choose()['card'],7)

    def test_hdmi_never_selected_even_if_only_card(self):
        (self.hw.sys/'class/sound/card1').unlink()
        result=self.hw.choose()
        self.assertEqual(result['id'],'integrated')
        self.assertFalse(result['ready'])
        self.assertIsNone(result['pcm'])

    def test_dp_iec958_and_digital_never_selected(self):
        (self.hw.sys/'class/sound/card1').unlink()
        self.hw.card(6,'DisplayPort','DisplayPort Audio')
        self.hw.card(8,'Digital','IEC958 Digital')
        self.assertFalse(self.hw.choose()['ready'])

    def test_dashboard_shows_active_card_and_integrated_standby(self):
        self.hw.usb('1-2','1235','8211','FOC123')
        self.hw.card(2,'BlueBridge','USB Audio', usb='1-2')
        selected, found = discover_outputs(
            dict(scarlett=dict(vendor='1235',product='8211',serial='FOC123')),
            sysroot=self.hw.sys, devroot=self.hw.dev, procroot=self.hw.proc)
        self.assertEqual(selected['id'], 'scarlett')
        self.assertEqual([x['id'] for x in found], ['scarlett','integrated'])
        cards = dashboard_candidates(found, selected['id'], in_use=True)
        self.assertEqual([x['state'] for x in cards], ['En uso','En espera'])
        self.assertEqual(sum(1 for x in cards if x['in_use']),1)
        self.assertEqual([x['priority'] for x in cards], [100,-100])
        paused = dashboard_candidates(found, selected['id'], in_use=False)
        self.assertEqual(paused[0]['state'], 'Seleccionada')
        self.assertFalse(any(x['in_use'] for x in paused))

    def test_dashboard_future_card_ranked_above_scarlett(self):
        self.hw.usb('1-2','1235','8211','FOC123')
        self.hw.card(2,'BlueBridge','USB Audio',usb='1-2')
        self.hw.usb('2-3','5678','90ab','PREMIUM')
        self.hw.card(4,'StudioNew','Studio USB Audio',usb='2-3')
        extra=dict(id='studio',label='Studio',vendor='5678',product='90ab',
                   serial='PREMIUM',priority=110,pcm_device=0)
        selected, found = discover_outputs(
            dict(scarlett=dict(vendor='1235',product='8211',serial='FOC123')),
            [extra],sysroot=self.hw.sys,devroot=self.hw.dev,procroot=self.hw.proc)
        self.assertEqual(selected['id'],'studio')
        self.assertEqual([x['id'] for x in found],['studio','scarlett','integrated'])
        cards=dashboard_candidates(found,'studio',True)
        self.assertEqual(sum(x['in_use'] for x in cards),1)
        self.assertEqual(cards[1]['state'],'En espera')
        self.assertNotIn('HDMI', str(cards))

    def test_unregistered_usb_device_ignored(self):
        self.hw.usb('2-3','9876','fedc','OTHER')
        self.hw.card(6,'USB1','USB Audio',usb='2-3')
        self.assertEqual(self.hw.choose()['id'],'integrated')

    def test_future_professional_higher_priority_auto_selected(self):
        self.hw.usb('1-2','1235','8211','FOC123')
        self.hw.card(2,'BlueBridge','USB Audio',usb='1-2')
        self.hw.usb('2-3','5678','90ab','PREMIUM')
        self.hw.card(4,'StudioNew','Studio USB Audio',usb='2-3')
        new=dict(id='studio',label='Studio',vendor='5678',product='90ab',serial='PREMIUM',
                 priority=110,pcm_device=0)
        self.assertEqual(self.hw.choose([new])['id'],'studio')
        new['priority']=90
        self.assertEqual(self.hw.choose([new])['id'],'scarlett')

    def test_future_professional_blocks_fallback_when_present_not_ready(self):
        self.hw.usb('2-3','5678','90ab','PREMIUM')
        new=dict(id='studio',label='Studio',vendor='5678',product='90ab',serial='PREMIUM',
                 priority=110,pcm_device=0)
        o=self.hw.choose([new])
        self.assertEqual(o['id'],'studio')
        self.assertFalse(o['ready'])

    def test_future_professional_needs_unique_playback_pcm(self):
        self.hw.usb('2-3','5678','90ab','PREMIUM')
        self.hw.card(4,'StudioNew','Studio USB Audio',usb='2-3')
        self.hw.card(4,'StudioNew','Studio USB Audio 2',device=1,usb='2-3')
        new=dict(id='studio',label='Studio',vendor='5678',product='90ab',serial='PREMIUM',priority=110)
        self.assertFalse(self.hw.choose([new])['ready'])
        new['pcm_device']=0
        self.assertTrue(self.hw.choose([new])['ready'])

    def test_future_professional_cannot_select_hdmi_named_pcm(self):
        self.hw.usb('2-3','5678','90ab','PREMIUM')
        self.hw.card(4,'StudioNew','HDMI / DisplayPort',usb='2-3')
        new=dict(id='studio',label='Studio',vendor='5678',product='90ab',serial='PREMIUM',priority=110,pcm_device=0)
        self.assertFalse(self.hw.choose([new])['ready'])

    def test_profile_validation(self):
        profile=dict(id='studio',label='Studio',vendor='5678',product='90ab',priority=110)
        self.assertEqual(_profiles([profile])[0]['id'],'studio')
        with self.assertRaises(ValueError): _profiles([profile,profile])
        with self.assertRaises(ValueError): _profiles([{**profile,'priority':2001}])


class RegressionTests(unittest.TestCase):
    def test_pcm_nonzero_device_path(self):
        with tempfile.TemporaryDirectory() as d:
            path=pathlib.Path(d)/'asound/card5/pcm1p/sub0/status'
            path.parent.mkdir(parents=True)
            path.write_text('state: RUNNING\nhw_ptr: 123\n')
            self.assertEqual(pcm_status(5,procroot=d,device=1)['state'],'RUNNING')
    def test_xrun_recovery_unchanged(self):
        r=XrunRecovery()
        self.assertFalse(r.observe(1,True,True))
        self.assertTrue(r.observe(17,True,True))
        self.assertFalse(r.observe(18,True,True))
    def test_dashboard_readonly_and_shows_selected(self):
        import bluebridge_dashboard
        html=bluebridge_dashboard.card('test')
        self.assertNotIn('Salida seleccionada',html)
        self.assertIn('<details id="bb-advanced"',html)
        self.assertIn('devices.filter(d=>d.selected)',html)
        self.assertIn('Tarjetas disponibles',html)
        self.assertIn('audio_devices',html)
        self.assertIn('renderAudioDevices',html)
        self.assertNotIn('Emparejar 120 s',html)
        self.assertNotIn('bb-open',html)
    def test_controller_preserves_v21_pairing_and_owner_rules(self):
        code=(ROOT/'controller.py').read_text()
        self.assertIn("'NoInputNoOutput'",code)
        self.assertIn('choose_audio_owner',code)
        self.assertIn('.Disconnect(',code)
        self.assertNotIn('ConnectProfile(',code)
        self.assertIn('audio_device=selected_audio',code)
    def test_player_switched_output_not_fixed_scarlett(self):
        code=(ROOT/'player.py').read_text()
        self.assertIn('discover_outputs(settings, profiles)',code)
        self.assertIn("output_pcm = 'bb_volume' if volume_supported is True else current['pcm']", code)
        self.assertIn('create_and_prime(current)', code)
        self.assertNotIn("'-D', 'plughw:CARD=BlueBridge,DEV=0'",code)
    def test_compile_everything(self):
        for name in ('core.py','audio_select.py','player.py','controller.py','bluebridge_dashboard.py'):
            compile((ROOT/name).read_text(),name,'exec')

if __name__=='__main__': unittest.main(verbosity=2)
