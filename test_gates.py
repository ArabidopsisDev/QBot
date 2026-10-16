"""Offline contract checks for the proposed gate pipeline; no QQ transport."""
import unittest

import gates


class GatePipelineTests(unittest.TestCase):
    def event(self, text='', *, mid=1, user=800000, group=312579123,
              at=None, image=0, nickname='普通昵称', stamp=1000):
        segments = []
        if at is not None:
            segments.append({'type':'at','data':{'qq':at,'name':'淫荡昵称'}})
        if text:
            segments.append({'type':'text','data':{'text':text}})
        segments.extend({'type':'image','data':{'file':'ignored'}} for _ in range(image))
        return {'post_type':'message','message_type':'group','group_id':group,
                'user_id':user,'self_id':900000,'message_id':mid,'time':stamp,
                'sender':{'nickname':nickname,'card':'普通群名片'},
                'message':segments,'raw_message':'绝对不读取淫荡'}

    def evaluate(self,event,history=None,**kwargs):
        env=gates.normalize_event(event,group_id=event['group_id'],self_id=900000)
        self.assertIsNotNone(env)
        return gates.evaluate(env,now=env.timestamp,history=history,**kwargs)

    def test_00_has_no_report_notice_or_output(self):
        decision=self.evaluate(self.event('淫荡'*50),banned=True)
        self.assertTrue(decision.blocked)
        self.assertEqual(decision.reason,'00')
        self.assertIsNone(decision.case)
        self.assertIsNone(decision.notice)
        self.assertEqual(decision.findings,[])

    def test_nickname_and_at_metadata_do_not_trigger_sensitive_gate(self):
        event=self.event('正文正常',at=123456,nickname='淫荡昵称')
        decision=self.evaluate(event)
        self.assertFalse(any(f.gate=='02' for f in decision.findings))
        self.assertFalse(decision.blocked)

    def test_length_gate_is_silent_and_excludes_at_segment(self):
        decision=self.evaluate(self.event('甲'*67,at=123456))
        self.assertEqual(decision.finding('01').category,'01a')
        self.assertTrue(decision.blocked)
        self.assertIsNone(decision.notice)

    def test_short_repeat_only_records_shadow_and_stays_silent(self):
        decision=self.evaluate(self.event('喵喵喵喵'))
        self.assertEqual(decision.finding('04').category,'04a')
        self.assertFalse(decision.blocked)
        self.assertIsNone(decision.notice)

    def test_sensitive_term_blocks_models_without_automatic_ban_or_notice(self):
        decision=self.evaluate(self.event('色情'))
        self.assertTrue(decision.blocked)
        self.assertFalse(decision.ban_applied)
        self.assertEqual(decision.finding('02').category,'02Ia')
        self.assertIsNone(decision.notice)
        self.assertEqual(decision.case['status'],'pending')

    def test_explicit_disapproval_keeps_review_but_proposes_no_penalty(self):
        decision=self.evaluate(self.event('我只是不喜欢黄色笑话，并不是不懂'))
        finding=decision.finding('02')
        self.assertIsNotNone(finding)
        self.assertTrue(decision.blocked)
        self.assertEqual(finding.suggested_points,0)
        self.assertEqual(finding.suggested_ban_seconds,0)
        self.assertIn('explicit_discussion_or_disapproval',finding.flags)
        direct=self.evaluate(self.event('你这个鸡巴'))
        self.assertGreater(direct.finding('02').suggested_points,0)
        uncertain=self.evaluate(self.event('我不喜欢你的鸡巴'))
        self.assertGreater(uncertain.finding('02').suggested_points,0)

    def test_expressive_repetition_is_zero_penalty_without_erasing_detection(self):
        decision=self.evaluate(self.event('喵'*10))
        finding=decision.finding('04')
        self.assertEqual(finding.category,'04b')
        self.assertEqual(finding.suggested_points,0)
        self.assertIn('expressive_repetition',finding.flags)
        self.assertIsNone(decision.notice)
        mechanical=self.evaluate(self.event('甲乙丙'*6))
        self.assertEqual(mechanical.finding('04').suggested_points,1)

    def test_four_similar_short_messages_do_not_prove_test_behavior(self):
        history=gates.GateHistory()
        for i in range(3):
            event=self.event('大家好呀',mid=i+1,stamp=1000+i)
            env=gates.normalize_event(event,group_id=312579123,self_id=900000)
            history.add(gates.record_for(env,time=env.timestamp),now=env.timestamp)
        decision=self.evaluate(self.event('群',mid=4,stamp=1003),history)
        self.assertIsNone(decision.finding('06'))

    def test_one_symbol_never_counts_as_training_probe(self):
        decision=self.evaluate(self.event('{'))
        self.assertIsNone(decision.finding('06'))

    def test_split_short_messages_are_only_suspected(self):
        history=gates.GateHistory()
        for i,text in enumerate(('这是第一段很长的文字为了测试能否分段处理甲乙丙丁戊己庚辛',
                                  '这是第二段很长的文字为了测试能否分段处理甲乙丙丁戊己庚辛',
                                  '这是第三段很长的文字为了测试能否分段处理甲乙丙丁戊己庚辛')):
            event=self.event(text,mid=i+1,stamp=1000+i)
            env=gates.normalize_event(event,group_id=312579123,self_id=900000)
            history.add(gates.record_for(env,time=env.timestamp),now=env.timestamp)
        decision=self.evaluate(self.event('这是第四段很长的文字为了测试能否分段处理甲乙丙丁戊己庚辛',
                                          mid=4,stamp=1003),history)
        self.assertEqual(decision.finding('01').category,'01s')
        self.assertFalse(decision.blocked)
        self.assertIsNone(decision.notice)

    def test_history_limit_is_per_user_even_when_interleaved(self):
        history=gates.GateHistory(limit=2)
        for mid,user in enumerate((800000,800001,800000,800001,800000),1):
            event=self.event('你好',mid=mid,user=user,stamp=1000+mid)
            env=gates.normalize_event(event,group_id=312579123,self_id=900000)
            history.add(gates.record_for(env,time=env.timestamp),now=env.timestamp)
        self.assertEqual([r.message_id for r in history.recent('312579123','800000',1010)],
                         ['3','5'])
        self.assertEqual([r.message_id for r in history.recent('312579123','800001',1010)],
                         ['2','4'])

    def test_invalid_timestamp_is_rejected(self):
        for stamp in ('bad',float('nan'),-1):
            event=self.event('你好',stamp=stamp)
            self.assertIsNone(gates.normalize_event(event,group_id=312579123,self_id=900000))


if __name__=='__main__':
    unittest.main()
