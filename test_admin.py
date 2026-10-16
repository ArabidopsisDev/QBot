import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import admin
import moderation
from qbot import GROUP_DATABASES, Bot, PRODUCTION_GROUP, TEST_GROUP, THIRD_GROUP


class AdminTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data = Path(self.temporary.name)
        self.pre = self.data / 'pre'
        (self.pre / 'text').mkdir(parents=True)
        self.bots = {}
        for group, filename in GROUP_DATABASES.items():
            bot = Bot(str(self.data / filename), self_id='900000', group_id=group,
                      pre_root=self.pre)
            self.addCleanup(bot.close)
            self.bots[group] = bot
        for attribute, value in (('DATA_ROOT', self.data), ('PRE_ROOT', self.pre)):
            patcher = patch.object(admin, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def get(self, path, **query):
        return admin.handle_api('GET', path, query, {})

    def post(self, path, **body):
        return admin.handle_api('POST', path, {}, body)

    def seed(self, group=TEST_GROUP, pairs=(('a', 'b', 100), ('c', 'd', 100))):
        db = self.bots[group].db
        with db:
            db.executemany('INSERT INTO chars VALUES (?,?,?)', pairs)

    def weight(self, first, second, group=TEST_GROUP):
        row = self.bots[group].db.execute(
            'SELECT w FROM chars WHERE first=? AND second=?', (first, second)).fetchone()
        return row[0] if row else None

    # ---- overview / dictionary / system ---------------------------------

    def test_overview_covers_every_group(self):
        status, payload = self.get('/api/overview')
        self.assertEqual(status, 200)
        self.assertEqual({row['group'] for row in payload['groups']}, set(GROUP_DATABASES))
        for row in payload['groups']:
            self.assertEqual(row['model'], 'match')
            self.assertEqual(row['bans'], 0)
            self.assertIn('label', row)

    def test_overview_reports_learned_volume(self):
        self.seed()
        status, payload = self.get('/api/overview')
        row = next(item for item in payload['groups'] if item['group'] == TEST_GROUP)
        self.assertEqual(row['transitions'], 2)
        self.assertEqual(row['transition_weight'], 200)

    def test_dictionary_lists_every_grade(self):
        status, payload = self.get('/api/dictionary')
        self.assertEqual(status, 200)
        self.assertEqual([item['grade'] for item in payload['grades']],
                         list(moderation.GRADES))
        self.assertEqual(len(payload['grades']), 13)
        self.assertEqual(payload['grades'][0]['seconds'], 60)
        self.assertEqual(payload['grades'][-1]['seconds'], 86400)
        for item in payload['grades']:
            self.assertTrue(item['terms'])
        self.assertTrue(payload['safe'])
        self.assertEqual(payload['escalation_window'], 2592000)

    def test_gate_catalog_lists_all_projects_and_live_modes(self):
        status,payload=self.get('/api/gates')
        self.assertEqual(status,200)
        self.assertEqual([row['number'] for row in payload['gates']],
                         ['00','01','02','03','04','05','06','07'])
        by_number={row['number']:row for row in payload['gates']}
        self.assertEqual(by_number['00']['notification'],'始终静默')
        self.assertTrue(any(item['code']=='01s' and item['mode']=='shadow'
                            for item in by_number['01']['subtypes']))
        self.assertEqual(len(by_number['02']['subtypes']),13)

    def test_system_state_reports_database_sizes(self):
        status, payload = self.get('/api/system')
        self.assertEqual(status, 200)
        self.assertEqual(len(payload['databases']), len(GROUP_DATABASES))
        self.assertFalse(payload['stop'])

    def test_unknown_route_and_group_are_rejected(self):
        self.assertEqual(self.get('/api/nope')[0], 404)
        self.assertEqual(self.get('/api/bans', group='nope')[0], 400)
        self.assertEqual(self.get('/api/generate', group='nope')[0], 400)

    # ---- bans ------------------------------------------------------------

    def test_manual_ban_and_unban_round_trip(self):
        status, payload = self.post('/api/ban', group=TEST_GROUP, user_id='700001',
                                    seconds=600, reason='后台测试')
        self.assertEqual(status, 200)
        self.assertTrue(payload['ok'])
        status, payload = self.get('/api/bans', group=TEST_GROUP)
        self.assertEqual(len(payload['bans']), 1)
        self.assertEqual(payload['bans'][0]['user_id'], '700001')
        self.assertEqual(payload['bans'][0]['reason'], '后台测试')
        self.assertEqual(payload['bans'][0]['mode'], '后台封禁')
        status, payload = self.post('/api/unban', group=TEST_GROUP, user_id='700001')
        self.assertEqual(payload['removed'], 1)
        self.assertEqual(self.get('/api/bans', group=TEST_GROUP)[1]['bans'], [])

    def test_ban_validation(self):
        self.assertEqual(self.post('/api/ban', group='nope', user_id='700001',
                                    seconds=60)[0], 400)
        self.assertEqual(self.post('/api/ban', group=TEST_GROUP, user_id='abc',
                                    seconds=60)[0], 400)
        self.assertEqual(self.post('/api/ban', group=TEST_GROUP, user_id='700001',
                                    seconds='x')[0], 400)
        self.assertEqual(self.post('/api/ban', group=TEST_GROUP, user_id='700001',
                                    seconds=0)[0], 400)
        self.assertEqual(self.bots[TEST_GROUP].db.execute(
            'SELECT COUNT(*) FROM bans').fetchone()[0], 0)

    def test_dstx_lifts_every_group(self):
        for group in GROUP_DATABASES:
            with self.bots[group].db as db:
                db.execute("INSERT INTO bans VALUES ('700001',9e9,60,'x','后台封禁',1)")
        status, payload = self.post('/api/dstx')
        self.assertEqual(status, 200)
        self.assertEqual(payload['released'], ['700001'])
        for group in GROUP_DATABASES:
            self.assertEqual(self.bots[group].db.execute(
                'SELECT COUNT(*) FROM bans').fetchone()[0], 0)

    def test_whitelist_grant_and_revoke(self):
        status, _ = self.post('/api/whitelist/grant', group=TEST_GROUP,
                              user_id='700002', seconds=90)
        self.assertEqual(status, 200)
        rows = self.get('/api/bans', group=TEST_GROUP)[1]['whitelist']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['user_id'], '700002')
        self.assertGreater(rows[0]['remaining'], 0)
        self.assertEqual(self.post('/api/whitelist/revoke', group=TEST_GROUP,
                                   user_id='700002')[1]['removed'], 1)
        self.assertEqual(self.get('/api/bans', group=TEST_GROUP)[1]['whitelist'], [])

    def test_whitelist_validation(self):
        self.assertEqual(self.post('/api/whitelist/grant', group=TEST_GROUP,
                                   user_id='700002', seconds=5)[0], 400)
        self.assertEqual(self.post('/api/whitelist/grant', group=TEST_GROUP,
                                   user_id='bad', seconds=90)[0], 400)

    def test_model_switch(self):
        status, payload = self.post('/api/model', group=TEST_GROUP, model='generate')
        self.assertEqual(status, 200)
        self.assertEqual(self.bots[TEST_GROUP].model, 'generate')
        self.assertEqual(self.post('/api/model', group=TEST_GROUP, model='nope')[0], 400)

    # ---- generate model --------------------------------------------------

    def test_generate_statistics_report_a_chinese_only_view(self):
        with self.bots[TEST_GROUP].db as db:
            db.executemany('INSERT INTO chars VALUES (?,?,?)', [
                ('甲', '乙', 100), ('丙', '丁', 100), ('.', 'j', 40)])
        status, payload = self.get('/api/generate', group=TEST_GROUP)
        self.assertEqual(status, 200)
        # 图表只统计中文，'.j' 这类不进入任何统计
        self.assertEqual(payload['transitions'], 2)
        self.assertEqual(payload['counted_weight'], 200)
        # 但权重调整的百分比基数是真实总权值，不能被裁剪
        self.assertEqual(payload['total_weight'], 240)
        self.assertEqual(payload['excluded_weight'], 40)
        self.assertEqual(payload['max_weight'], 100)
        self.assertEqual(payload['distinct_first'], 2)
        self.assertEqual(payload['top'][0]['w'], 100)
        self.assertAlmostEqual(payload['top'][0]['share'], 0.5)
        self.assertEqual(sum(payload['histogram']['counts']), 2)

    def test_explore_builds_heatmap_and_graph(self):
        with self.bots[TEST_GROUP].db as db:
            db.executemany('INSERT INTO chars VALUES (?,?,?)', [
                ('甲', '乙', 100), ('丙', '甲', 50)])
        status, payload = self.get('/api/generate/explore', group=TEST_GROUP, char='甲')
        self.assertEqual(status, 200)
        self.assertEqual(payload['char'], '甲')
        self.assertEqual([row['second'] for row in payload['successors']], ['乙'])
        self.assertEqual([row['first'] for row in payload['predecessors']], ['丙'])
        # The centre character leads the heatmap rows, then its predecessors.
        self.assertEqual(payload['heatmap']['rows'], ['甲', '丙'])
        self.assertEqual(payload['heatmap']['cols'], ['乙'])
        self.assertEqual(payload['heatmap']['values'], [[100], [0]])
        kinds = {node['kind'] for node in payload['graph']['nodes']}
        self.assertEqual(kinds, {'center', 'prev', 'next'})
        self.assertEqual(len(payload['graph']['links']), 2)
        self.assertEqual(payload['out_total'], 100)

    def test_explore_ignores_retained_non_counted_rows(self):
        with self.bots[TEST_GROUP].db as db:
            db.executemany('INSERT INTO chars VALUES (?,?,?)', [
                ('甲', '乙', 10), ('甲', '.', 900), ('.', '丙', 900)])
        payload = self.get('/api/generate/explore', group=TEST_GROUP, char='甲')[1]
        # generate() never walks these rows, so the view must not show them either.
        self.assertEqual([row['second'] for row in payload['successors']], ['乙'])
        self.assertEqual(payload['out_total'], 10)
        self.assertEqual(payload['model_total'], 10)
        self.assertEqual(payload['successors'][0]['share'], 1.0)
        # ...but they are still in the table, untouched.
        self.assertEqual(self.weight('甲', '.'), 900)
        self.assertEqual(self.weight('.', '丙'), 900)

    def test_explore_includes_chinese_punctuation_transitions(self):
        with self.bots[TEST_GROUP].db as db:
            db.executemany('INSERT INTO chars VALUES (?,?,?)', [
                ('甲', '乙', 10), ('甲', '，', 5), ('甲', '。', 5)])
        payload = self.get('/api/generate/explore', group=TEST_GROUP, char='甲')[1]
        self.assertEqual([row['second'] for row in payload['successors']], ['乙', '，', '。'])
        self.assertEqual(payload['out_total'], 20)

    def test_explore_rejects_a_bad_character(self):
        self.assertEqual(self.get('/api/generate/explore', group=TEST_GROUP,
                                  char='') [0], 400)
        self.assertEqual(self.get('/api/generate/explore', group=TEST_GROUP,
                                  char='abcde')[0], 400)

    def test_adjust_adds_a_percentage_of_the_total_weight(self):
        self.seed()
        status, payload = self.post('/api/generate/adjust', group=TEST_GROUP,
                                    mode='pair', first='a', second='b',
                                    direction='add', percent=10)
        self.assertEqual(status, 200)
        report = payload['report']
        self.assertEqual(report['delta'], 20)          # 10% of 200
        self.assertEqual(report['total_before'], 200)
        self.assertEqual(report['total_after'], 220)
        self.assertEqual(report['applied'], 1)
        self.assertEqual(self.weight('a', 'b'), 120)

    def test_adjust_reduction_below_zero_deletes_the_row(self):
        self.seed()
        status, payload = self.post('/api/generate/adjust', group=TEST_GROUP,
                                    mode='pair', first='a', second='b',
                                    direction='reduce', percent=60)
        self.assertEqual(status, 200)
        # delta is 120, which is more than the pair's 100, so the row is dropped.
        self.assertEqual(payload['report']['delta'], 120)
        self.assertEqual(payload['report']['deleted'], 1)
        self.assertIsNone(self.weight('a', 'b'))
        self.assertEqual(self.weight('c', 'd'), 100)   # 'pair' mode touches one pair only

    def test_adjust_sentence_touches_every_adjacent_pair(self):
        with self.bots[TEST_GROUP].db as db:
            db.execute("INSERT INTO chars VALUES ('起','来',100)")
        status, payload = self.post('/api/generate/adjust', group=TEST_GROUP,
                                    mode='sentence', sentence='起来了起来了',
                                    direction='add', percent=10)
        self.assertEqual(status, 200)
        self.assertEqual(payload['report']['applied'], 3)   # 起-来, 来-了, 了-起
        self.assertIsNotNone(self.weight('来', '了'))
        self.assertIsNotNone(self.weight('了', '起'))

    def test_adjust_validation(self):
        self.assertEqual(self.post('/api/generate/adjust', group=TEST_GROUP,
                                    mode='pair', first='a', second='b',
                                    direction='sideways', percent=5)[0], 400)
        self.assertEqual(self.post('/api/generate/adjust', group=TEST_GROUP,
                                    mode='pair', first='a', second='b',
                                    direction='add', percent=0)[0], 400)
        self.assertEqual(self.post('/api/generate/adjust', group=TEST_GROUP,
                                    mode='pair', first='', second='b',
                                    direction='add', percent=5)[0], 400)
        self.assertEqual(self.post('/api/generate/adjust', group=TEST_GROUP,
                                    mode='sentence', sentence='a',
                                    direction='add', percent=5)[0], 400)
        self.assertEqual(self.post('/api/generate/adjust', group=TEST_GROUP,
                                    mode='weird', direction='add', percent=5)[0], 400)

    # ---- training tools --------------------------------------------------

    def test_train_from_the_web_panel(self):
        (self.pre / 'text' / '甲.txt').write_text(
            '2024-01-01 10:00:00 甲(111111)\n你好世界\n'
            '2024-01-01 10:00:10 乙(222222)\n今天天气不错\n', encoding='utf-8')
        status, payload = self.post('/api/train', group=TEST_GROUP, name='甲')
        self.assertEqual(status, 200)
        self.assertEqual(payload['records'], 2)
        self.assertEqual(payload['trained'], 2)
        self.assertEqual(self.get('/api/sources')[1]['sources'], ['甲'])
        # The import must survive the temporary bot being closed.
        # 你好世界 -> 3 transitions, 今天天气不错 -> 5 transitions.
        self.assertEqual(self.get('/api/generate', group=TEST_GROUP)[1]['total_weight'], 8)
        self.assertGreater(self.bots[TEST_GROUP].db.execute(
            'SELECT COUNT(*) FROM intel_memory').fetchone()[0], 0)

    def test_train_reports_a_missing_source(self):
        status, payload = self.post('/api/train', group=TEST_GROUP, name='不存在')
        self.assertEqual(status, 400)
        self.assertIn('未找到文件', payload['error'])

    def test_save_and_load_round_trip(self):
        self.seed()
        before = self.get('/api/generate', group=TEST_GROUP)[1]['total_weight']
        status, payload = self.post('/api/save', group=TEST_GROUP, name='网备')
        self.assertEqual(status, 200)
        self.assertEqual(payload['summary']['chars'], 2)
        self.assertEqual(self.post('/api/clear', group=TEST_GROUP,
                                   model='generate')[0], 200)
        self.assertEqual(self.get('/api/generate', group=TEST_GROUP)[1]['total_weight'], 0)
        status, payload = self.post('/api/load', group=TEST_GROUP, name='网备')
        self.assertEqual(status, 200)
        self.assertEqual(payload['report']['chars'], 2)
        self.assertEqual(self.get('/api/generate', group=TEST_GROUP)[1]['total_weight'], before)

    def test_save_rejects_an_escaping_name(self):
        status, payload = self.post('/api/save', group=TEST_GROUP, name='../evil')
        self.assertEqual(status, 400)
        self.assertIn('名称无效', payload['error'])

    def say(self, bot, text, user):
        self.sequence = getattr(self, 'sequence', 0) + 1
        return bot.handle(dict(
            post_type='message', message_type='group', group_id=TEST_GROUP,
            self_id='900000', user_id=user, message_id=str(self.sequence),
            time=float(self.sequence), message=[{'type': 'text', 'data': {'text': text}}]))

    # ---- per-sender scoping ---------------------------------------------

    def test_learning_attributes_transitions_to_the_sender(self):
        bot = self.bots[TEST_GROUP]
        self.say(bot, '你好世界', '700001')
        self.say(bot, '今天天气不错', '700002')
        status, payload = self.get('/api/generate/users', group=TEST_GROUP)
        self.assertEqual(status, 200)
        self.assertEqual({row['user_id'] for row in payload['senders']},
                         {'700001', '700002'})
        self.assertEqual(payload['attribution']['coverage'], 1.0)
        self.assertEqual(payload['attribution']['senders'], 2)

    def test_generate_can_be_filtered_by_sender(self):
        bot = self.bots[TEST_GROUP]
        self.say(bot, '你好世界', '700001')
        self.say(bot, '今天天气不错', '700002')
        status, payload = self.get('/api/generate', group=TEST_GROUP, user='700001')
        self.assertEqual(status, 200)
        self.assertEqual(payload['scope'], 'user')
        self.assertEqual(payload['total_weight'], 3)
        self.assertEqual({row['first'] for row in payload['top']}, {'你', '好', '世'})
        # The other sender's characters must not leak into this scope.
        self.assertNotIn('今', {row['first'] for row in payload['top']})

    def test_explore_can_be_filtered_by_sender(self):
        bot = self.bots[TEST_GROUP]
        self.say(bot, '你好世界', '700001')
        self.say(bot, '你好吗', '700002')
        status, payload = self.get('/api/generate/explore', group=TEST_GROUP,
                                   char='好', user='700002')
        self.assertEqual(status, 200)
        self.assertEqual(payload['scope'], 'user')
        self.assertEqual([row['second'] for row in payload['successors']], ['吗'])
        self.assertEqual(payload['heatmap']['rows'], ['好', '你'])
        # Centre row 好→吗 is that sender's own transition; 你→吗 is not present.
        self.assertEqual(payload['heatmap']['values'], [[1], [0]])

    def test_attribution_coverage_reports_unattributed_data(self):
        self.seed()                      # Written straight into chars, no sender.
        self.say(self.bots[TEST_GROUP], '你好世界', '700001')
        attribution = self.get('/api/generate', group=TEST_GROUP)[1]['attribution']
        self.assertEqual(attribution['total_weight'], 203)
        self.assertEqual(attribution['attributed_weight'], 3)
        self.assertLess(attribution['coverage'], 0.02)

    def test_pair_detail_lists_contributors(self):
        bot = self.bots[TEST_GROUP]
        self.say(bot, '你好世界', '700001')
        self.say(bot, '你好世界', '700002')
        status, payload = self.get('/api/generate/pair', group=TEST_GROUP,
                                   first='你', second='好')
        self.assertEqual(status, 200)
        self.assertEqual(payload['weight'], 2)
        self.assertEqual(payload['rank'], 1)
        self.assertEqual(payload['distinct_users'], 2)
        self.assertEqual({row['user_id'] for row in payload['contributors']},
                         {'700001', '700002'})
        self.assertEqual(payload['attributed_weight'], 2)

    def test_pair_detail_rejects_a_missing_pair_argument(self):
        self.assertEqual(self.get('/api/generate/pair', group=TEST_GROUP,
                                  first='你')[0], 400)

    def test_train_attributes_imported_transitions(self):
        (self.pre / 'text' / '甲.txt').write_text(
            '2024-01-01 10:00:00 甲(111111)\n你好世界\n', encoding='utf-8')
        status, payload = self.post('/api/train', group=TEST_GROUP, name='甲')
        self.assertEqual(status, 200)
        self.assertEqual(payload['attributed'], 3)
        senders = self.get('/api/generate/users', group=TEST_GROUP)[1]['senders']
        self.assertEqual([row['user_id'] for row in senders], ['111111'])
        self.assertEqual(senders[0]['transitions'], 3)

    def test_training_learns_every_pair_while_display_filters(self):
        bot = self.bots[TEST_GROUP]
        self.say(bot, '你好，世界！ab1', '700001')
        trained = {tuple(row) for row in bot.db.execute('SELECT first,second FROM chars')}
        # Training keeps everything -- punctuation included -- so replies read well.
        self.assertEqual(trained, {('你', '好'), ('好', '，'), ('，', '世'), ('世', '界'),
                                   ('界', '！'), ('！', 'a'), ('a', 'b'), ('b', '1')})
        attributed = {tuple(row) for row in bot.db.execute(
            'SELECT first,second FROM user_chars')}
        self.assertEqual(attributed, trained)
        # The dashboard, however, reports a Chinese-only view.
        preview = self.get('/api/generate/non_chinese', group=TEST_GROUP)[1]
        self.assertEqual(preview['rows'], 3)          # ！a / ab / b1
        self.assertEqual(preview['total_rows'], 8)
        payload = self.get('/api/generate', group=TEST_GROUP)[1]
        self.assertEqual(payload['transitions'], 5)   # 你好，世界！

    def test_non_counted_transitions_are_reported_but_never_removed(self):
        with self.bots[TEST_GROUP].db as db:
            db.executemany('INSERT INTO chars VALUES (?,?,?)', [
                ('你', '好', 100), ('.', 'j', 40), ('好', '。', 30), ('码', '.', 10)])
            db.execute("INSERT INTO user_chars VALUES ('700001','.','j',5)")
        status, preview = self.get('/api/generate/non_chinese', group=TEST_GROUP)
        self.assertEqual(status, 200)
        # 中文标点计入，所以只有 '.j' 与 '码.' 这两行是「不计入」的。
        self.assertEqual(preview['rows'], 2)
        self.assertEqual(preview['weight'], 50)
        self.assertEqual(preview['total_rows'], 4)
        self.assertEqual(preview['total_weight'], 180)
        self.assertAlmostEqual(preview['weight_share'], 0.2778)
        self.assertEqual(preview['samples'][0]['first'], '.')
        # Nothing is deleted: the panel is read-only by design.
        self.assertEqual(self.weight('.', 'j'), 40)
        self.assertEqual(self.weight('你', '好'), 100)
        self.assertEqual(self.get('/api/generate', group=TEST_GROUP)[1]['total_weight'], 180)
        self.assertEqual(self.post('/api/generate/purge', group=TEST_GROUP)[0], 404)
        self.assertEqual(self.bots[TEST_GROUP].db.execute(
            'SELECT COUNT(*) FROM user_chars').fetchone()[0], 1)

    def test_explore_reports_lift_concentration_and_effective_support(self):
        with self.bots[TEST_GROUP].db as db:
            db.executemany('INSERT INTO chars VALUES (?,?,?)', [
                ('你', '好', 60), ('你', '吗', 20), ('你', '的', 20),
                ('我', '你', 50), ('他', '你', 50)])
        status, payload = self.get('/api/generate/explore', group=TEST_GROUP, char='你')
        self.assertEqual(status, 200)
        self.assertEqual(payload['model_total'], 200)
        rows = {row['second']: row for row in payload['successors']}
        # out_total = 100; 好 的边际权重 60 → 0.6 / 0.3 = 2.0
        self.assertEqual(rows['好']['share'], 0.6)
        self.assertEqual(rows['好']['marginal'], 60)
        self.assertEqual(rows['好']['marginal_share'], 0.3)
        self.assertEqual(rows['好']['lift'], 2.0)
        # 先驱同样带 lift：我 → 你 占 0.5，而 我 的边际占 0.25
        self.assertEqual(payload['predecessors'][0]['lift'], 2.0)
        self.assertAlmostEqual(payload['distribution_entropy'], 1.371, places=3)
        self.assertAlmostEqual(payload['effective_support'], 2.586, places=2)
        self.assertEqual(payload['concentration']['top80'], 2)
        self.assertEqual(payload['concentration']['cumulative'], [0.6, 0.8, 1.0])
        self.assertEqual(payload['concentration']['count'], 3)

    def test_explore_respects_the_requested_scope(self):
        bot = self.bots[TEST_GROUP]
        self.say(bot, '你好世界', '700001')
        self.say(bot, '你好吗', '700002')
        scoped = self.get('/api/generate/explore', group=TEST_GROUP, char='你',
                          user='700002')[1]
        self.assertEqual(scoped['scope'], 'user')
        self.assertEqual(scoped['model_total'], 2)   # 你好吗 → 3 个字，2 个相邻对
        self.assertEqual([row['second'] for row in scoped['successors']], ['好'])

    # ---- loop sweep ------------------------------------------------------

    def memory_id(self, text):
        row = self.bots[TEST_GROUP].db.execute(
            'SELECT id FROM intel_memory WHERE text=?', (text,)).fetchone()
        self.assertIsNotNone(row, f'{text!r} was not learned')
        return row[0]

    def test_loop_detection_is_exact_not_heuristic(self):
        profile = admin.loop_profile
        self.assertEqual(profile('一万世明后一万世明后一万世明后'), (1.0, 5, 3))
        self.assertEqual(profile('不不不不'), (1.0, 1, 4))
        self.assertEqual(profile('好的好的好的'), (1.0, 2, 3))
        # Ordinary sentences must stay well under the threshold.
        for text in ('今天天气不错', '你好好学习天天向上', '这个方案我觉得可以'):
            with self.subTest(text=text):
                self.assertLess(profile(text)[0], admin.LOOP_MIN_SCORE)
        for short in ('', '啊', 'abc'):
            with self.subTest(short=short):
                self.assertEqual(profile(short)[0], 0.0)

    def old_loop(self, text='甲乙丙丁戊己'*4, group=TEST_GROUP):
        # Seed legacy observations directly: new live training repairs repeats.
        bot = self.bots[group]
        bot.learn_chars(text, '700001')
        bot.learn_trigrams(text)
        bot.intelligence.observe(text, 1)
        bot.db.commit()
        return bot.db.execute('SELECT id FROM intel_memory WHERE text=?', (text,)).fetchone()[0]

    def clean_loop(self, target, group=TEST_GROUP):
        status, plan = self.post('/api/loops/preview', group=group, ids=[target])
        self.assertEqual(status, 200)
        return self.post('/api/loops/clean', group=group, ids=[target], plan_token=plan['plan_token'])

    def test_loop_rows_preserve_short_emphasis_and_laughter(self):
        self.old_loop()
        bot = self.bots[TEST_GROUP]
        for text in ('哈哈'*30, '喵'*30, '好的好的好的', '今天天气不错'):
            bot.intelligence.observe(text, 2)
        bot.db.commit()
        status, payload = self.get('/api/loops', group=TEST_GROUP)
        self.assertEqual(status, 200)
        self.assertEqual(payload['group'], TEST_GROUP)
        self.assertEqual([row['text'] for row in payload['loops']], ['甲乙丙丁戊己'*4])

    def test_loop_clean_preserves_all_character_tables_and_backups(self):
        import sqlite3
        from contextlib import closing
        target = self.old_loop()
        db = self.bots[TEST_GROUP].db
        before = {table: db.execute('SELECT * FROM '+table+' ORDER BY 1,2').fetchall()
                  for table in ('chars','gen_trigrams','user_chars')}
        status, report = self.clean_loop(target)
        self.assertEqual(status, 200)
        self.assertEqual(report['sentences_removed'], 1)
        self.assertEqual(report['weight_removed'], 0)
        self.assertTrue(Path(report['backup']).is_file())
        for table, rows in before.items():
            self.assertEqual(db.execute('SELECT * FROM '+table+' ORDER BY 1,2').fetchall(), rows)
        with closing(sqlite3.connect(report['backup'])) as backup:
            self.assertIsNotNone(backup.execute('SELECT id FROM intel_memory WHERE id=?',(target,)).fetchone())

    def test_loop_clean_does_not_subtract_fresh_weights_after_generate_clear(self):
        target = self.old_loop()
        db = self.bots[TEST_GROUP].db
        db.execute('DELETE FROM chars'); db.execute('DELETE FROM gen_trigrams')
        self.bots[TEST_GROUP].learn_chars('甲乙正常', '700002'); db.commit()
        status, report = self.clean_loop(target)
        self.assertEqual(status, 200)
        self.assertEqual(self.weight('甲', '乙'), 1)

    def test_loop_clean_removes_only_selected_links_and_qa(self):
        text = '甲乙丙丁戊己'*4
        target = self.old_loop(text)
        bot = self.bots[TEST_GROUP]
        bot.intelligence.observe('今天天气不错', 2)
        bot.db.execute('INSERT INTO qa VALUES (?,?,1)', (text,'今天天气不错'))
        bot.db.execute('INSERT INTO qa VALUES (?,?,1)', ('你好','世界'))
        bot.db.commit()
        status, report = self.clean_loop(target)
        self.assertEqual(status, 200)
        self.assertEqual(report['qa_rows_removed'], 1)
        self.assertIsNone(bot.db.execute('SELECT id FROM intel_memory WHERE id=?',(target,)).fetchone())
        self.assertIsNone(bot.db.execute('SELECT qid FROM intel_links WHERE qid=? OR aid=?',(target,target)).fetchone())
        self.assertIsNone(bot.db.execute('SELECT memory_id FROM intel_terms WHERE memory_id=?',(target,)).fetchone())
        self.assertIsNotNone(bot.db.execute("SELECT 1 FROM qa WHERE q='你好'").fetchone())

    def test_loop_clean_requires_current_preview_for_the_same_group(self):
        target = self.old_loop()
        other = self.old_loop(group=THIRD_GROUP)
        plan = self.post('/api/loops/preview', group=TEST_GROUP, ids=[target])[1]
        self.assertEqual(self.post('/api/loops/clean', group=THIRD_GROUP, ids=[other],
                                   plan_token=plan['plan_token'])[0], 400)
        db = self.bots[TEST_GROUP].db
        db.execute('UPDATE intel_memory SET w=w+1 WHERE id=?',(target,)); db.commit()
        self.assertEqual(self.post('/api/loops/clean', group=TEST_GROUP, ids=[target],
                                   plan_token=plan['plan_token'])[0], 400)

    def test_loop_clean_fails_closed_when_backup_fails(self):
        target = self.old_loop()
        with patch('loop_cleanup.backup_database', side_effect=OSError('offline failure')):
            status, _ = self.clean_loop(target)
        self.assertEqual(status, 400)
        self.assertIsNotNone(self.bots[TEST_GROUP].db.execute('SELECT id FROM intel_memory WHERE id=?',(target,)).fetchone())

    def test_loop_preview_accepts_large_raw_ids_and_rejects_regular_prose(self):
        target = self.old_loop()
        db = self.bots[TEST_GROUP].db
        # The actual dashboard bug happened for every ID >=1000.
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('UPDATE intel_memory SET id=14997 WHERE id=?',(target,))
        db.execute('UPDATE intel_terms SET memory_id=14997 WHERE memory_id=?',(target,))
        db.commit()
        self.assertEqual(self.post('/api/loops/preview', group=TEST_GROUP, ids=[14997])[0], 200)
        self.assertEqual(self.post('/api/loops/preview', group=TEST_GROUP, ids=['14,997'])[0], 400)
        db.execute("INSERT INTO intel_memory(text,w,last_seen) VALUES ('今天天气不错',1,2)"); db.commit()
        healthy = self.memory_id('今天天气不错')
        self.assertEqual(self.post('/api/loops/preview', group=TEST_GROUP, ids=[healthy])[0], 400)

    def test_loop_endpoints_validate_input(self):
        self.assertEqual(self.get('/api/loops', group='nope')[0], 400)
        self.assertEqual(self.post('/api/loops/preview', group=TEST_GROUP, ids=[])[0], 400)
        self.assertEqual(self.post('/api/loops/preview', group=TEST_GROUP, ids=['x'])[0], 400)
        self.assertEqual(self.post('/api/loops/clean', group=TEST_GROUP, ids=[99999])[0], 400)

    def test_pair_ban_crud_reports_the_model_weight(self):
        status, _ = self.post('/api/pairbans', group=TEST_GROUP,
                              first='甲', second='乙', reason='测试原因')
        self.assertEqual(status, 200)
        bans = self.get('/api/pairbans', group=TEST_GROUP)[1]['bans']
        self.assertEqual(len(bans), 1)
        self.assertEqual((bans[0]['first'], bans[0]['second'], bans[0]['reason']),
                         ('甲', '乙', '测试原因'))
        self.assertIsNone(bans[0]['weight'])        # 模型里还没有这一对
        with self.bots[TEST_GROUP].db as db:
            db.execute("INSERT INTO chars VALUES ('甲','乙',7)")
        bans = self.get('/api/pairbans', group=TEST_GROUP)[1]['bans']
        self.assertEqual(bans[0]['weight'], 7)
        # 重复添加是更新而不是报错
        self.assertEqual(self.post('/api/pairbans', group=TEST_GROUP,
                                   first='甲', second='乙', reason='改了')[0], 200)
        bans = self.get('/api/pairbans', group=TEST_GROUP)[1]['bans']
        self.assertEqual(len(bans), 1)
        self.assertEqual(bans[0]['reason'], '改了')
        self.assertEqual(self.post('/api/pairbans/delete', group=TEST_GROUP,
                                   first='甲', second='乙')[1]['removed'], 1)
        self.assertEqual(self.get('/api/pairbans', group=TEST_GROUP)[1]['bans'], [])

    def test_pair_ban_validation_and_per_group_isolation(self):
        for first, second in (('甲乙', '丙'), ('', '丙'), ('甲', '乙丙'), ('甲', '')):
            with self.subTest(first=first, second=second):
                self.assertEqual(self.post('/api/pairbans', group=TEST_GROUP,
                                           first=first, second=second)[0], 400)
        self.assertEqual(self.get('/api/pairbans', group='nope')[0], 400)
        self.post('/api/pairbans', group=PRODUCTION_GROUP, first='甲', second='乙')
        self.assertEqual(self.get('/api/pairbans', group=TEST_GROUP)[1]['bans'], [])
        self.assertEqual(len(self.get('/api/pairbans', group=PRODUCTION_GROUP)[1]['bans']), 1)

    def test_production_group_is_isolated_from_test_group(self):
        self.post('/api/ban', group=PRODUCTION_GROUP, user_id='700003', seconds=60)
        self.assertEqual(self.get('/api/bans', group=TEST_GROUP)[1]['bans'], [])
        self.assertEqual(len(self.get('/api/bans', group=PRODUCTION_GROUP)[1]['bans']), 1)
        self.assertEqual(self.get('/api/bans', group=THIRD_GROUP)[1]['bans'], [])


if __name__ == '__main__':
    unittest.main()
