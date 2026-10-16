"""Offline sticker tests: synthetic events and bytes, no QQ API or network."""
import io
import json
import hashlib
import hmac
import http.client
import tempfile
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import live
import sticker
from qbot import Bot, TEST_GROUP, PRODUCTION_GROUP, group_reply

PNG = (b'\x89PNG\r\n\x1a\n' + b'\x00\x00\x00\x0dIHDR' +
       b'\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00' +
       b'\x1f\x15\xc4\x89' + b'\x00\x00\x00\x0bIDAT' +
       b'x\x9cc\x00\x01\x00\x00\x05\x00\x01' +
       b'\xa5\xf6\x45\x40\x00\x00\x00\x00IEND\xaeB`\x82')


def event(message_id=1, *, group=TEST_GROUP, subtype=1,
          summary='[动画表情]', url='https://multimedia.nt.qq.com.cn/sticker.jpg'):
    return {'post_type':'message','message_type':'group','group_id':group,
            'self_id':'900000','user_id':'800000','message_id':message_id,
            'time':100.0,'message':[{'type':'image','data':{
                'file':'sticker.jpg','url':url,'sub_type':subtype,
                'summary':summary,'file_size':str(len(PNG))}}]}


class AlwaysTrigger:
    def random(self): return 0.0


class FixedDraw:
    def __init__(self,value): self.value=value
    def random(self): return self.value


class StickerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.library=sticker.StickerLibrary(self.root/'data'/'stickers',
            clock=lambda:200.0,rng=AlwaysTrigger(),fetch=lambda url:PNG)

    def test_only_explicit_stickers_and_known_https_hosts(self):
        self.assertIsNotNone(sticker.sticker_segment(event()))
        self.assertIsNone(sticker.sticker_segment(event(subtype=0,summary='[图片]')))
        self.assertIsNone(sticker.sticker_segment(event(subtype=1,summary='[图片]')))
        self.assertIsNone(sticker.sticker_segment(event(url='http://multimedia.nt.qq.com.cn/x')))
        self.assertIsNone(sticker.sticker_segment(event(url='https://evil.example/x')))
        self.assertIsNone(sticker.sticker_segment(event(url='https://multimedia.nt.qq.com.cn.evil.example/x')))
        mixed=event()
        mixed['message'].append({'type':'text','data':{'text':'caption'}})
        self.assertIsNone(sticker.sticker_segment(mixed))
        face=event()
        face['message'][0]['type']='face'
        self.assertIsNone(sticker.sticker_segment(face))
        market=event(url='https://gxh.vip.qq.com/club/item/example.gif',subtype=None,summary='开心')
        market['message'][0]['data'].update(emoji_id='e1',emoji_package_id='p1')
        self.assertEqual(sticker.sticker_segment(market)['kind'],'market')
        self.assertIsNone(sticker.sticker_segment(event(group='NOT-ALLOWED')))

    def test_capture_deduplicates_and_reuses_only_same_group_context(self):
        self.assertTrue(self.library.capture(event(1),('今天真好笑',99)))
        self.assertFalse(self.library.capture(event(1),('今天真好笑',99)))
        self.assertTrue(self.library.capture(event(2),('今天真好笑',99)))
        with self.library.connect() as db:
            row=db.execute('SELECT seen_count FROM assets WHERE group_id=?',(TEST_GROUP,)).fetchone()
            self.assertEqual(row,(2,))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM terms WHERE group_id=?',
                                        (TEST_GROUP,)).fetchone()[0]>0,True)
        selected=self.library.choose(TEST_GROUP,'今天真好笑')
        self.assertIsNotNone(selected)
        self.assertEqual(selected.read_bytes(),PNG)
        self.assertIsNone(self.library.choose(PRODUCTION_GROUP,'今天真好笑'))
        self.assertIsNone(self.library.choose(TEST_GROUP,'今天真好笑'))  # cooldown

    def test_single_custom_or_stale_context_does_not_trigger(self):
        self.library.capture(event(3),('今天真好笑',0))
        self.assertIsNone(self.library.choose(TEST_GROUP,'今天真好笑'))
        with self.library.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM terms').fetchone()[0],0)

    def test_generated_text_can_find_a_relevant_sticker_when_prompt_does_not(self):
        self.library.capture(event(21),('今天真好笑',99))
        self.library.capture(event(22),('今天真好笑',99))
        self.assertIsNone(self.library.choose(TEST_GROUP,'明天去哪里'))
        selected=self.library.choose(TEST_GROUP,'明天去哪里',generated='今天真好笑')
        self.assertIsNotNone(selected)
        self.assertEqual(selected.read_bytes(),PNG)
        self.assertIsNone(self.library.choose(TEST_GROUP,'明天去哪里',generated='今天真好笑'))

    def test_single_custom_requires_strong_generated_match_and_probability(self):
        self.library.capture(event(31),('今天真好笑',99))
        self.assertIsNone(self.library.choose(TEST_GROUP,'今天真好笑'))
        self.assertIsNone(self.library.choose(TEST_GROUP,'天气不错',generated='完全无关的文字'))
        self.library.rng=FixedDraw(.15)
        self.assertIsNone(self.library.choose(TEST_GROUP,'天气不错',generated='今天真好笑'))
        self.library.rng=FixedDraw(.05)
        self.assertIsNotNone(self.library.choose(TEST_GROUP,'天气不错',generated='今天真好笑'))

    def test_strong_reply_has_moderate_chance_and_three_minute_cooldown(self):
        self.library.capture(event(41),('今天真好笑',99))
        self.library.capture(event(42),('今天真好笑',99))
        self.library.rng=FixedDraw(.31)
        self.assertIsNone(self.library.choose(TEST_GROUP,'今天真好笑',generated='今天真好笑'))
        self.library.rng=FixedDraw(.29)
        self.assertIsNotNone(self.library.choose(TEST_GROUP,'今天真好笑',generated='今天真好笑'))
        self.library.clock=lambda:379.0
        self.assertIsNone(self.library.choose(TEST_GROUP,'今天真好笑',generated='今天真好笑'))
        self.library.clock=lambda:381.0
        self.assertIsNotNone(self.library.choose(TEST_GROUP,'今天真好笑',generated='今天真好笑'))

    def test_sender_uses_plain_image_segment_and_fixed_group(self):
        self.library.capture(event(1),('今天真好笑',99))
        image_path=next((self.root/'data'/'stickers'/TEST_GROUP).iterdir())
        limiter=Mock()
        limiter.acquire.return_value=True
        sender=live.Sender('offline-token',rate_limit=limiter)
        sender.opener=Mock()
        sender.opener.open.return_value=io.BytesIO(b'{"status":"ok","retcode":0}')
        with patch.object(live,'ROOT',self.root),patch.object(live,'STOP_FILE',self.root/'STOP'):
            self.assertTrue(sender.send_image(TEST_GROUP,image_path))
            request=sender.opener.open.call_args.args[0]
            body=json.loads(request.data)
            self.assertEqual(body['group_id'],int(TEST_GROUP))
            self.assertEqual(body['message'],[
                {'type':'image','data':{'file':str(image_path.resolve()),'sub_type':1}}])
            with self.assertRaises(PermissionError):
                sender.send_image('NOT-ALLOWED',image_path)
            outside=self.root/'ordinary.jpg'
            outside.write_bytes(PNG)
            with self.assertRaises(PermissionError):
                sender.send_image(TEST_GROUP,outside)
            self.assertEqual(sender.opener.open.call_count,1)

    def test_generated_reply_is_marked_but_commands_are_not(self):
        bot=Bot(self_id='900000')
        self.addCleanup(bot.close)
        command={'post_type':'message','message_type':'group','group_id':TEST_GROUP,
                 'self_id':'900000','user_id':'800000','message_id':1,'time':1,
                 'message':[{'type':'text','data':{'text':'/model generate'}}]}
        self.assertNotIn('model_generated',bot.handle(command))
        normal=dict(command,message_id=2,time=2,
                    message=[{'type':'text','data':{'text':'哈哈哈哈哈哈'}}])
        reply=bot.handle(normal)
        if reply is not None:
            self.assertIs(reply.get('model_generated'),True)

    def test_handler_sends_text_before_scheduling_sticker(self):
        sequence=[]
        bot=Mock(self_id='900000',previous=None)
        bot.db.total_changes=0
        bot.active_ban.return_value=None
        reply=group_reply(TEST_GROUP,'模型生成的文字')
        reply['model_generated']=True
        bot.handle.return_value=reply
        sender=Mock()
        sender.send.side_effect=lambda *a,**k:(sequence.append('text') or True)
        stickers=Mock()
        stickers.after_text.side_effect=lambda *a,**k:sequence.append('image_job')
        token='offline-secret'
        server=HTTPServer(('127.0.0.1',0),live.make_handler(
            {TEST_GROUP:bot},sender,token,stickers))
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            payload={'post_type':'message','message_type':'group',
                     'group_id':TEST_GROUP,'self_id':'900000',
                     'user_id':'800000','message_id':12,'time':100,
                     'message':[{'type':'text','data':{'text':'今天好笑'}}]}
            body=json.dumps(payload).encode()
            signature='sha1='+hmac.new(token.encode(),body,hashlib.sha1).hexdigest()
            connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
            with patch.object(live,'STOP_FILE',self.root/'STOP'):
                connection.request('POST','/events',body=body,
                                   headers={'x-signature':signature})
                response=connection.getresponse()
                self.assertEqual(response.status,200)
                response.read()
            connection.close()
            self.assertEqual(sequence,['text','image_job'])
            stickers.after_text.assert_called_once_with(
                TEST_GROUP,'今天好笑',None,'模型生成的文字')
            stickers.capture.assert_not_called()
        finally:
            server.shutdown()
            thread.join()
            server.server_close()


if __name__=='__main__':
    unittest.main()
