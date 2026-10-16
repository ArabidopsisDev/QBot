"""Offline tests for the optional two-character lookahead (next_gen.md phase 1).

Everything runs against in-memory or temp databases with synthetic text:
no QQ connection, no real group database, no network.
"""
import copy
import random
import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import datastore
from qbot import (Bot, CLEAR_ADMIN, GROUP_DATABASES, PRODUCTION_GROUP, TEST_GROUP,
                  THIRD_GROUP, FOURTH_GROUP, blend_limits, trigram_counts)


class Recorder:
    """Seeded rng that logs every draw, so fallback paths stay comparable."""

    def __init__(self, seed=7):
        self._rng = random.Random(seed)
        self.calls = []

    def randint(self, low, high):
        value = self._rng.randint(low, high)
        self.calls.append(('randint', low, high, value))
        return value

    def randrange(self, total, *rest):
        value = self._rng.randrange(total, *rest)
        self.calls.append(('randrange', total, value))
        return value

    def random(self):
        value = self._rng.random()
        self.calls.append(('random', value))
        return value


class AlwaysFirst:
    """Deterministic walk: fixed length, always the first sorted candidate."""

    def __init__(self, length=6):
        self.length = length
        self.calls = []

    def randint(self, low, high):
        assert (low, high) == (3, 50)
        self.calls.append(('randint', low, high))
        return self.length

    def randrange(self, total):
        self.calls.append(('randrange', total))
        return 0


class Harness:
    _issued = 0
    _clock = 0

    def __init__(self, bot, user='800000'):
        Harness._issued += 1
        self.bot = bot
        self.user = user
        self.serial = 0
        self.prefix = f'h{Harness._issued}-'

    def event(self, text, **changes):
        self.serial += 1
        Harness._clock += 1
        event = dict(post_type='message', message_type='group',
                     group_id=self.bot.group_id, self_id=self.bot.self_id,
                     user_id=self.user, message_id=self.prefix + str(self.serial),
                     time=float(Harness._clock),
                     message=[{'type': 'text', 'data': {'text': text}}])
        event.update(changes)
        return event

    def send(self, text, user=None, **changes):
        if user is not None:
            changes['user_id'] = user
        return self.bot.handle(self.event(text, **changes))

    def say(self, text, user=None, **changes):
        """Send and unwrap the reply text, so assertions read naturally."""
        result = self.send(text, user=user, **changes)
        return result['params']['message'][0]['data']['text'] if result else None


def trigram_rows(bot):
    return bot.db.execute(
        'SELECT older,first,second,w FROM gen_trigrams '
        'ORDER BY older,first,second').fetchall()


def pair_rows(bot):
    return bot.db.execute('SELECT first,second,w FROM chars '
                          'ORDER BY first,second').fetchall()


class TrigramLearningTests(unittest.TestCase):
    def setUp(self):
        self.bot = Bot(self_id='900000')
        self.addCleanup(self.bot.close)
        self.harness = Harness(self.bot)

    def test_window_count_matches_message_length(self):
        self.harness.send('ABC')
        self.assertEqual(trigram_rows(self.bot), [('A', 'B', 'C', 1)])
        rows = trigram_rows(self.bot)
        self.harness.send('XY')          # m-2 = 0 windows
        self.harness.send('Z')           # m-2 = 0 windows
        self.assertEqual(trigram_rows(self.bot), rows)

    def test_windows_never_span_two_messages(self):
        self.harness.send('AB')
        self.harness.send('BCD')
        self.assertEqual(trigram_rows(self.bot), [('B', 'C', 'D', 1)])
        self.assertNotIn(('A', 'B', 'C'), {row[:3] for row in trigram_rows(self.bot)})

    def test_scope_equals_learn_chars_including_punctuation_and_symbols(self):
        text = 'a!中🙂b。1'
        self.harness.send(text)
        self.assertEqual(len(pair_rows(self.bot)), len(text) - 1)
        self.assertEqual(len(trigram_rows(self.bot)), len(text) - 2)
        self.assertEqual({row[:3] for row in trigram_rows(self.bot)},
                         set(trigram_counts(text)))
        self.assertEqual({row[:3] for row in trigram_rows(self.bot)},
                         {('a', '!', '中'), ('!', '中', '🙂'), ('中', '🙂', 'b'),
                          ('🙂', 'b', '。'), ('b', '。', '1')})

    def test_self_duplicate_command_banned_and_other_group_are_not_learned(self):
        self.harness.send('甲乙丙')
        duplicate = self.harness.event('丁戊己')
        self.bot.handle(duplicate)
        self.bot.handle(copy.deepcopy(duplicate))          # same message id
        before = trigram_rows(self.bot)
        self.assertEqual(before, [('丁', '戊', '己', 1), ('甲', '乙', '丙', 1)])
        self.harness.send('庚辛壬', user='900000')          # the bot itself
        self.harness.send('辛壬癸', group_id=PRODUCTION_GROUP)   # another group
        self.assertEqual(trigram_rows(self.bot), before)
        self.harness.send('/model generate')               # a command
        self.assertEqual(trigram_rows(self.bot), before)
        trained_pairs = {row[:2] for row in pair_rows(self.bot)}
        self.assertNotIn(('m', 'o'), trained_pairs)        # the command text
        self.assertNotIn(('庚', '辛'), trained_pairs)      # the other group
        self.harness.send('/ban 800001 60', user=CLEAR_ADMIN)
        self.harness.send('癸子丑', user='800001')         # banned sender
        self.assertEqual(trigram_rows(self.bot), before)


class BlendProbabilityTests(unittest.TestCase):
    def setUp(self):
        self.bot = Bot(self_id='900000', generate_blend_max=0.20)
        self.addCleanup(self.bot.close)
        self.harness = Harness(self.bot)
        for _ in range(5):
            self.harness.send('好了啊')
        for _ in range(5):
            self.harness.send('坏了嗯')
        self.rows = self.bot.db.execute(
            "SELECT second,w FROM chars WHERE first='了' ORDER BY second").fetchall()
        self.assertEqual(self.rows, [('啊', 5), ('嗯', 5)])

    @staticmethod
    def distribution(blended):
        total = sum(weight for _, weight in blended)
        return {char: weight / total for char, weight in blended}

    def test_shared_tail_splits_by_the_earlier_character(self):
        good = self.bot.blend_candidates('好了', self.rows)
        bad = self.bot.blend_candidates('坏了', self.rows)
        self.assertEqual([char for char, _ in good], ['啊', '嗯'])
        self.assertEqual([char for char, _ in bad], ['啊', '嗯'])
        good_probability = self.distribution(good)
        bad_probability = self.distribution(bad)
        # lambda = 0.20 * 5/(5+4) applied to P1 = (0.5, 0.5).
        self.assertAlmostEqual(good_probability['啊'], 0.5 + 0.20 * 5 / 9 / 2, places=5)
        self.assertAlmostEqual(good_probability['嗯'], 0.5 - 0.20 * 5 / 9 / 2, places=5)
        self.assertAlmostEqual(bad_probability['啊'], good_probability['嗯'], places=5)
        self.assertAlmostEqual(bad_probability['嗯'], good_probability['啊'], places=5)
        self.assertGreater(abs(good_probability['啊'] - bad_probability['啊']), 0.05)
        # The single-character model keeps at least 80% of every candidate.
        for probability in (good_probability, bad_probability):
            for char in ('啊', '嗯'):
                self.assertGreaterEqual(probability[char] + 1e-9, 0.8 * 0.5)

    def test_no_trigram_evidence_falls_back_to_the_single_character_model(self):
        self.assertIsNone(self.bot.blend_candidates('甲了', self.rows))
        self.assertIsNone(self.bot.blend_candidates('了', self.rows))  # one-character hint
        self.assertIsNone(self.bot.blend_candidates('好了', []))
        with self.bot.db as db:
            db.execute('DELETE FROM gen_trigrams')
        self.assertIsNone(self.bot.blend_candidates('好了', self.rows))

    def test_trigram_candidates_outside_chars_are_filtered_away(self):
        baseline = self.bot.blend_candidates('好了', self.rows)
        with self.bot.db as db:
            db.execute("INSERT INTO gen_trigrams VALUES ('好','了','丁',100)")
        blended = self.bot.blend_candidates('好了', self.rows)
        # 丁 is not a successor `chars` allows, so it never enters P2 and the
        # surviving evidence is exactly what it was before the row appeared.
        self.assertEqual(dict(blended), dict(baseline))
        self.assertNotIn('丁', dict(blended))
        with self.bot.db as db:
            db.execute("DELETE FROM chars WHERE first='了' AND second='啊'")
        rows = self.bot.db.execute(
            "SELECT second,w FROM chars WHERE first='了' ORDER BY second").fetchall()
        # The only remaining evidence points at a deleted edge: fall back fully.
        self.assertEqual(rows, [('嗯', 5)])
        self.assertIsNone(self.bot.blend_candidates('好了', rows))

    def test_switch_bounds(self):
        with self.assertRaises(ValueError):
            Bot(self_id='900000', generate_blend_max=0.21)
        with self.assertRaises(ValueError):
            Bot(self_id='900000', generate_blend_max=-0.1)
        default = Bot(self_id='900000')
        self.addCleanup(default.close)
        self.assertEqual(default.generate_blend_max, 0.0)
        self.assertIsNone(default.blend_candidates('好了', self.rows))
        enabled = Bot(self_id='900000', generate_blend_max=0.2)
        self.addCleanup(enabled.close)
        self.assertEqual(enabled.generate_blend_max, 0.2)


class BlendGenerationTests(unittest.TestCase):
    def build(self, **kwargs):
        bot = Bot(self_id='900000', **kwargs)
        self.addCleanup(bot.close)
        return bot, Harness(bot)

    @staticmethod
    def corpus(bot):
        harness = Harness(bot)
        harness.send('甲乙丙甲乙丙')
        harness.send('乙丙丁')
        return harness

    def test_default_switch_keeps_the_old_walk_and_random_stream(self):
        with_trigrams, _ = self.build(rng=Recorder(11))
        without, _ = self.build(rng=Recorder(11))
        self.corpus(with_trigrams)
        self.corpus(without)
        with with_trigrams.db as db:
            db.execute("INSERT OR REPLACE INTO gen_trigrams VALUES ('甲','乙','丙',500)")
            db.execute("INSERT OR REPLACE INTO gen_trigrams VALUES ('X','Y','Z',500)")
        with without.db as db:
            db.execute('DELETE FROM gen_trigrams')
        outputs, traces = [], []
        for bot in (with_trigrams, without):
            bot.rng = Recorder(11)
            outputs.append([bot.generate(prompt) for prompt in ('甲乙', '乙丙', '丙丁')])
            traces.append(list(bot.rng.calls))
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(traces[0], traces[1])

    def test_one_random_draw_per_generated_character(self):
        bot, _ = self.build(generate_blend_max=0.20)
        with bot.db as db:
            db.executemany('INSERT INTO chars VALUES (?,?,?)',
                           [('甲', '乙', 4), ('乙', '丙', 4), ('丙', '甲', 4)])
            db.executemany('INSERT INTO gen_trigrams VALUES (?,?,?,?)',
                           [('甲', '乙', '丙', 4), ('乙', '丙', '甲', 4),
                            ('丙', '甲', '乙', 4)])
        bot.rng = Recorder(3)
        result = bot.generate('甲乙')
        draws = [call for call in bot.rng.calls if call[0] == 'randrange']
        self.assertEqual(len(draws), len(result))
        for call in draws:
            self.assertGreater(call[1], 0)  # one exact integer draw per generated character

    def test_rare_existing_successor_is_never_rounded_away(self):
        bot, _ = self.build(generate_blend_max=0.20)
        with bot.db as db:
            db.executemany('INSERT INTO chars VALUES (?,?,?)',
                           [('了', '常', 3000000), ('了', '稀', 1)])
            db.execute("INSERT INTO gen_trigrams VALUES ('好','了','常',10)")
        rows = bot.db.execute("SELECT second,w FROM chars WHERE first='了' ORDER BY second").fetchall()
        mixed = dict(bot.blend_candidates('好了', rows))
        self.assertEqual(set(mixed), {'常', '稀'})
        self.assertGreater(mixed['稀'], 0)
        self.assertGreaterEqual(mixed['稀'] / sum(mixed.values()),
                                (1 - 0.20) / 3000001)

    def test_length_bounds_seed_is_context_only_and_pair_ban_still_cuts(self):
        bot, _ = self.build(generate_blend_max=0.20)
        with bot.db as db:
            db.executemany('INSERT INTO chars VALUES (?,?,?)',
                           [('甲', '乙', 4), ('乙', '丙', 4), ('丙', '甲', 4)])
            db.executemany('INSERT INTO gen_trigrams VALUES (?,?,?,?)',
                           [('甲', '乙', '丙', 4), ('乙', '丙', '甲', 4),
                            ('丙', '甲', '乙', 4)])
        for length in (3, 50, 7):
            bot.rng = AlwaysFirst(length)
            result = bot.generate('甲乙')
            self.assertEqual(len(result), length)
            self.assertTrue(result.startswith('丙'))       # the seed is not prepended
            self.assertFalse(result.startswith('甲乙'))
        bot.rng = AlwaysFirst(6)
        self.assertIsNone(bot.generate('丁戊'))            # no successor at all
        with bot.db as db:
            db.execute("INSERT INTO pair_bans VALUES ('丙','甲','测试',0)")
        bot.rng = AlwaysFirst(6)
        self.assertIsNone(bot.generate('甲乙'))            # banned pair aborts
        with bot.db as db:
            db.execute("DELETE FROM pair_bans")
        bot.rng = AlwaysFirst(6)
        self.assertEqual(bot.generate('甲乙'), '丙甲乙丙甲乙')

    def test_reply_is_still_rate_limited_and_the_two_hint_characters_are_used(self):
        bot, harness = self.build(generate_blend_max=0.20)
        harness.send('/model generate')
        with patch.object(bot, 'generate', return_value='合成回复') as mocked:
            result = harness.send('好了')
        mocked.assert_called_with('好了')
        self.assertNotIn('exempt', result)                 # the 5-second limit applies
        self.assertEqual(result['params']['group_id'], int(TEST_GROUP))
        with patch.object(bot, 'generate', return_value='合成回复') as mocked:
            harness.send('甲')
        mocked.assert_called_with('甲')

    def test_banned_sender_is_neither_trained_nor_answered(self):
        bot, harness = self.build(generate_blend_max=0.20)
        harness.send('/model generate')
        harness.send('甲乙丙', user='700001')
        before = trigram_rows(bot)
        harness.send('/ban 800002 600', user=CLEAR_ADMIN)
        notice = harness.send('丁戊己', user='800002')
        self.assertIsNone(notice)
        self.assertEqual(trigram_rows(bot), before)
        harness.send('/unban 800002', user=CLEAR_ADMIN)
        harness.send('庚辛壬', user='800002')
        self.assertNotEqual(trigram_rows(bot), before)


class TrainAndPretrainTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bot = Bot(self_id='900000', pre_root=self.root)
        self.addCleanup(self.bot.close)
        self.harness = Harness(self.bot, user=CLEAR_ADMIN)

    def write_source(self, reference, content):
        path = datastore.source_file_for(self.root, reference)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        return path

    def test_train_uses_the_same_scope_as_live_learning(self):
        self.write_source('样例', '2024-01-01 10:00:00 甲(111111)\n你好世界\n')
        self.harness.send('/train 样例')
        trained = trigram_rows(self.bot)
        self.bot.db.execute('DELETE FROM gen_trigrams')
        self.bot.db.execute('DELETE FROM chars')
        Harness(self.bot, user='700001').send('你好世界')
        self.assertEqual(trained, trigram_rows(self.bot))
        self.assertEqual(trained, [('你', '好', '世', 1), ('好', '世', '界', 1)])

    def test_train_never_spans_an_attachment_placeholder(self):
        self.write_source('附件', '2024-01-01 10:00:00 甲(111111)\n甲乙丙[图片]丁戊己\n')
        self.harness.send('/train 附件')
        windows = {row[:3] for row in trigram_rows(self.bot)}
        self.assertEqual(windows, {('甲', '乙', '丙'), ('丁', '戊', '己')})
        self.assertNotIn(('丙', '丁', '戊'), windows)

    def test_train_rejects_commands_and_own_messages(self):
        self.write_source('过滤',
            '2024-01-01 10:00:00 甲(111111)\n/model generate\n'
            '2024-01-01 10:00:05 机器人(900000)\n我是机器人\n'
            '2024-01-01 10:00:10 乙(222222)\n正常内容\n')
        self.harness.send('/train 过滤')
        self.assertEqual({row[:3] for row in trigram_rows(self.bot)},
                         {('正', '常', '内'), ('常', '内', '容')})

    def test_pretrain_is_idempotent_and_an_old_marker_stops_reimport(self):
        import pretrain
        source = self.root / 'export.txt'
        texts = ['early'] * 8 + ['好了啊[图片]坏了嗯', '甲乙丙']
        source.write_text('\n\n'.join(
            f'2026-01-01 00:00:{index:02d} User(12345)\n{text}'
            for index, text in enumerate(texts)), encoding='utf-8-sig')
        first = self.root / 'first.sqlite3'
        summary = pretrain.train(source, first)
        self.assertFalse(summary['already_imported'])
        self.assertGreater(summary['unique_trigrams'], 0)
        with closing(sqlite3.connect(first)) as connection:
            trigram_weight = connection.execute(
                'SELECT SUM(w) FROM gen_trigrams').fetchone()[0]
            pair_weight = connection.execute('SELECT SUM(w) FROM chars').fetchone()[0]
        self.assertTrue(pretrain.train(source, first)['already_imported'])
        with closing(sqlite3.connect(first)) as connection:
            self.assertEqual(connection.execute(
                'SELECT SUM(w) FROM gen_trigrams').fetchone()[0], trigram_weight)
            self.assertEqual(connection.execute(
                'SELECT SUM(w) FROM chars').fetchone()[0], pair_weight)
        # A marker written by an older release short-circuits before any insert.
        with closing(sqlite3.connect(first)) as connection:
            identity = connection.execute('SELECT id FROM pretraining').fetchone()[0]
        old = self.root / 'old.sqlite3'
        Bot(str(old), self_id='schema-only').close()
        with closing(sqlite3.connect(old)) as connection:
            connection.execute('CREATE TABLE pretraining '
                               '(id TEXT PRIMARY KEY, summary TEXT NOT NULL)')
            connection.execute('INSERT INTO pretraining VALUES (?,?)', (identity, '{}'))
            connection.commit()
        self.assertTrue(pretrain.train(source, old)['already_imported'])
        with closing(sqlite3.connect(old)) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM chars').fetchone()[0], 0)
            self.assertEqual(connection.execute(
                'SELECT COUNT(*) FROM gen_trigrams').fetchone()[0], 0)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bot = Bot(self_id='900000', pre_root=self.root)
        self.addCleanup(self.bot.close)
        self.harness = Harness(self.bot, user=CLEAR_ADMIN)
        self.harness.send('甲乙丙')

    def test_group_clear_commands_only_drop_the_generate_tables(self):
        before = trigram_rows(self.bot)
        self.harness.send('/clear match')
        self.assertEqual(trigram_rows(self.bot), before)
        self.harness.send('/clear intelligence')
        self.assertEqual(trigram_rows(self.bot), before)
        self.harness.send('/clear generate')
        self.assertEqual(trigram_rows(self.bot), [])
        self.assertEqual(pair_rows(self.bot), [])

    def test_other_groups_keep_their_own_trigrams(self):
        production = Bot(self_id='900000', group_id=PRODUCTION_GROUP)
        self.addCleanup(production.close)
        Harness(production, user=CLEAR_ADMIN).send('丁戊己')
        self.harness.send('/clear generate')
        self.assertEqual(trigram_rows(self.bot), [])
        self.assertEqual([row[:3] for row in trigram_rows(production)],
                         [('丁', '戊', '己')])

    def test_save_load_round_trip_including_the_new_table(self):
        before = trigram_rows(self.bot)
        self.assertIn('已保存', self.harness.say('/save 备份'))
        self.harness.say('/clear generate')
        self.assertEqual(trigram_rows(self.bot), [])
        self.assertIn('已加载', self.harness.say('/load 备份'))
        self.assertEqual(trigram_rows(self.bot), before)

    def test_backup_written_before_the_table_loads_as_an_empty_one(self):
        payload = datastore.export_database(self.bot.db, group_id=TEST_GROUP, saved_at=1.0)
        del payload['gen_trigrams']
        self.assertEqual(payload['version'], datastore.FORMAT_VERSION)
        chars_before = pair_rows(self.bot)
        self.bot.db.execute('DELETE FROM gen_trigrams')
        self.bot.db.execute('DELETE FROM chars')
        stats = datastore.import_database(self.bot.db, payload)
        self.assertEqual(stats['gen_trigrams'], 0)
        self.assertEqual(pair_rows(self.bot), chars_before)
        self.assertEqual(trigram_rows(self.bot), [])

    def test_illegal_trigram_rows_are_rejected_without_touching_the_model(self):
        payload = datastore.export_database(self.bot.db, group_id=TEST_GROUP, saved_at=1.0)
        chars_before, trigrams_before = pair_rows(self.bot), trigram_rows(self.bot)
        illegal = ([['ab', 'c', 'd', 1]], [['a', 'b', 'c', 0]],
                   [['a', 'b', 'c', -1]], [['a', 'b', 'c', '1']],
                   [['a', 'b', 'c', 1.0]], [['a', 'b', 'c']], ['x'], 'nope')
        for bad in illegal:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                broken = dict(payload)
                broken['gen_trigrams'] = bad
                datastore.import_database(self.bot.db, broken)
            self.assertEqual(pair_rows(self.bot), chars_before)
            self.assertEqual(trigram_rows(self.bot), trigrams_before)

    def test_backup_with_pretraining_markers_still_loads(self):
        before = trigram_rows(self.bot)
        with self.bot.db as db:
            db.execute('CREATE TABLE IF NOT EXISTS pretraining '
                       '(id TEXT PRIMARY KEY, summary TEXT NOT NULL)')
            db.execute("INSERT INTO pretraining VALUES ('marker','{}')")
        payload = datastore.export_database(self.bot.db, group_id=TEST_GROUP, saved_at=1.0)
        stats = datastore.import_database(self.bot.db, payload)
        self.assertEqual(stats['pretraining'], 1)
        self.assertEqual(trigram_rows(self.bot), before)


class AdminClearTests(unittest.TestCase):
    def setUp(self):
        import admin
        self.admin = admin
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data = Path(self.temporary.name)
        self.bots = {}
        for group, filename in GROUP_DATABASES.items():
            bot = Bot(str(self.data / filename), self_id='900000', group_id=group)
            self.addCleanup(bot.close)
            self.bots[group] = bot
        for attribute, value in (('DATA_ROOT', self.data), ('PRE_ROOT', self.data / 'pre')):
            patcher = patch.object(admin, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        Harness(self.bots[TEST_GROUP], user=CLEAR_ADMIN).send('甲乙丙')

    def clear(self, model):
        return self.admin.handle_api('POST', '/api/clear', {},
                                     {'group': TEST_GROUP, 'model': model})

    def test_panel_clear_drops_only_the_generate_trigrams(self):
        self.assertGreater(len(trigram_rows(self.bots[TEST_GROUP])), 0)
        self.assertEqual(self.clear('match')[0], 200)
        self.assertGreater(len(trigram_rows(self.bots[TEST_GROUP])), 0)
        self.assertEqual(self.clear('intelligence')[0], 200)
        self.assertGreater(len(trigram_rows(self.bots[TEST_GROUP])), 0)
        self.assertEqual(self.clear('generate')[0], 200)
        self.assertEqual(trigram_rows(self.bots[TEST_GROUP]), [])
        status, payload = self.admin.handle_api('GET', '/api/overview', {}, {})
        self.assertEqual(status, 200)
        row = next(item for item in payload['groups'] if item['group'] == TEST_GROUP)
        self.assertEqual(row['trigrams'], 0)
        self.assertEqual(row['trigram_weight'], 0)


class ConfigTests(unittest.TestCase):
    def test_missing_key_defaults_every_group_to_zero(self):
        limits = blend_limits({})
        self.assertEqual(set(limits), {TEST_GROUP, PRODUCTION_GROUP, THIRD_GROUP, FOURTH_GROUP})
        self.assertEqual(set(limits.values()), {0.0})

    def test_per_group_values_are_accepted_only_within_bounds(self):
        limits = blend_limits({'generate_blend_max': {TEST_GROUP: 0.2,
                                                      PRODUCTION_GROUP: 0}})
        self.assertEqual(limits[TEST_GROUP], 0.2)
        self.assertEqual(limits[PRODUCTION_GROUP], 0.0)
        self.assertEqual(limits[THIRD_GROUP], 0.0)
        self.assertEqual(limits[FOURTH_GROUP], 0.0)

    def test_invalid_entries_are_refused(self):
        illegal = ({'generate_blend_max': []},
                   {'generate_blend_max': {TEST_GROUP: 0.25}},
                   {'generate_blend_max': {TEST_GROUP: -1}},
                   {'generate_blend_max': {TEST_GROUP: float('nan')}},
                   {'generate_blend_max': {TEST_GROUP: float('inf')}},
                   {'generate_blend_max': {'42': 0.1}},
                   {'generate_blend_max': '0.1'},
                   'nope', None)
        for value in illegal:
            with self.subTest(value=value), self.assertRaises(ValueError):
                blend_limits(value)


if __name__ == '__main__':
    unittest.main()
