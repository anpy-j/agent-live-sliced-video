import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from agent_video.pipeline.editorial import (
    PLAN_SCHEMA, REVIEW_SCHEMA, digest, require_release, review_and_revise,
    save, validate_plan, complete_dependencies, dependency_blocks, compose,
)
from agent_video.pipeline.errors import AIReturnError
from agent_video.pipeline.run import _validate_order
from agent_video.db import Store


def plan(ids, topics=None):
    topics = topics or ['fabric'] * len(ids)
    return {'main_product': '毛衣', 'opening_topic': topics[0], 'ordered_ids': ids,
            'sections': [{'role': 'hook' if n == 0 else 'proof', 'topic': topic,
                          'ids': [cid]} for n, (cid, topic) in enumerate(zip(ids, topics))]}


class EditorialTest(unittest.TestCase):
    def test_compose_collapses_equivalent_units_but_keeps_complementary_facts(self):
        self.labels[1]['facts'] = ['0']
        def call(model, prompt, schema, timeout):
            data = json.loads(prompt.split('\n')[-1])
            self.assertEqual({c['id'] for c in data['candidates']}, {0, 2, 3})
            return plan([0, 2])
        compose(self.candidates, self.labels, 'mock', 10, call, (10, 20), 1, _validate_order)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.candidates = [{'id': i, 'text': str(i), 'start': float(i * 10),
                            'end': float(i * 10 + 5)} for i in range(4)]
        self.labels = {i: {'id': i, 'topic': 'fabric', 'facts': [str(i)],
                          'eligible': True, 'requires': [], 'subject': '毛衣',
                          'reason': '新增信息'} for i in range(4)}

    def validate(self, data):
        return validate_plan(data, self.candidates, self.labels, (10, 20), 1, _validate_order)

    def review(self, data, call, target=(10, 20)):
        self.validate(data)
        return review_and_revise(data, self.candidates, self.labels, 'mock', 10,
                                 call, target, 1, _validate_order, self.tmp.name)

    def issue(self, cid=2, action='drop'):
        return {'passed': False, 'issues': [{'ids': [cid], 'kind': 'low_value',
                                            'reason': '表达空泛', 'action': action}]}

    def test_topic_return_and_opening_mismatch_are_rejected(self):
        self.labels[1]['topic'] = 'color'
        with self.assertRaises(AIReturnError):
            self.validate(plan([0, 1, 2], ['fabric', 'color', 'fabric']))
        data = plan([0])
        data['opening_topic'] = 'color'
        with self.assertRaises(AIReturnError):
            self.validate(data)

    def test_duplicate_facts_and_ineligible_content_are_rejected(self):
        self.labels[1]['facts'] = ['0']
        with self.assertRaises(AIReturnError):
            self.validate(plan([0, 1]))
        self.labels[1]['facts'] = ['1']
        self.labels[1]['eligible'] = False
        with self.assertRaises(AIReturnError):
            self.validate(plan([0, 1]))

    def test_dependency_must_follow_immediately(self):
        self.labels[0]['requires'] = [1]
        with self.assertRaises(AIReturnError):
            self.validate(plan([0, 2, 1]))
        self.validate(plan([0, 1, 2]))

    def test_previous_dependency_stays_before_the_dependent_sentence(self):
        self.labels[1]['requires'] = [0]
        self.validate(plan([0, 1]))
        completed = complete_dependencies(plan([1, 2]), self.candidates, self.labels)
        self.assertEqual(completed['ordered_ids'], [0, 1, 2])
        self.validate(completed)

    def test_forward_dependency_is_added_in_source_order(self):
        self.labels[0]['requires'] = [1]
        completed = complete_dependencies(plan([0, 2]), self.candidates, self.labels)
        self.assertEqual(completed['ordered_ids'], [0, 1, 2])
        self.validate(completed)

    def test_shared_dependency_is_included_once_in_a_contiguous_group(self):
        self.labels[1]['requires'] = [0]
        self.labels[2]['requires'] = [0]
        completed = complete_dependencies(plan([2, 3, 1]), self.candidates, self.labels)
        self.assertEqual(completed['ordered_ids'], [0, 1, 2, 3])
        self.validate(completed)

    def test_unusable_cross_topic_and_cyclic_dependency_units_are_excluded(self):
        self.labels[1]['requires'] = [0]
        self.assertNotIn(1, dependency_blocks(self.candidates, self.labels, excluded=[0]))
        self.labels[0]['topic'] = 'color'
        self.assertNotIn(1, dependency_blocks(self.candidates, self.labels))
        self.labels[0]['topic'] = 'fabric'
        self.labels[0]['requires'] = [1]
        self.assertNotIn(1, dependency_blocks(self.candidates, self.labels))

    def test_composition_retries_an_invalid_unit_with_a_fixed_upper_bound(self):
        self.labels[1]['requires'] = [0]
        self.labels[0]['eligible'] = False
        call = Mock(side_effect=[plan([1]), plan([2, 3])])
        result = compose(self.candidates, self.labels, 'mock', 10, call, (10, 20), 1, _validate_order)
        self.assertEqual(result['ordered_ids'], [2, 3])
        self.assertEqual(call.call_count, 2)
        for invoked in call.call_args_list:
            data = json.loads(invoked.args[1].splitlines()[-1])
            self.assertEqual([c['id'] for c in data['candidates']], [2, 3])
        call = Mock(return_value=plan([1]))
        with self.assertRaises(AIReturnError):
            compose(self.candidates, self.labels, 'mock', 10, call, (10, 20), 1, _validate_order)
        self.assertEqual(call.call_count, 2)

    def test_in_range_drop_keeps_other_sentences_without_composition(self):
        call = Mock(side_effect=[self.issue(), {'passed': True, 'issues': []}])
        state = self.review(plan([0, 1, 2]), call)
        self.assertEqual(state['plan']['ordered_ids'], [0, 1])
        self.assertEqual(state['released'], 'passed')
        self.assertEqual(call.call_count, 2)
        self.assertTrue(all(c.args[2] is REVIEW_SCHEMA for c in call.call_args_list))

    def test_below_minimum_refills_from_original_pool_and_excludes_removed(self):
        def call(model, prompt, schema, timeout):
            if schema is PLAN_SCHEMA:
                data = json.loads(prompt.splitlines()[-1])
                self.assertNotIn(1, [c['id'] for c in data['candidates']])
                self.assertIn(3, [c['id'] for c in data['candidates']])
                return plan([0, 3])
            if '"ordered_ids": [0, 1]' in prompt:
                return self.issue(1)
            return {'passed': True, 'issues': []}
        state = self.review(plan([0, 1]), Mock(side_effect=call))
        self.assertEqual(state['plan']['ordered_ids'], [0, 3])
        self.assertEqual(state['excluded'], [1])

    def test_passed_short_script_still_refills_when_new_facts_exist(self):
        call = Mock(side_effect=[{'passed': True, 'issues': []}, plan([0, 1]),
                                 {'passed': True, 'issues': []}])
        state = self.review(plan([0]), call)
        self.assertEqual(state['plan']['ordered_ids'], [0, 1])
        self.assertEqual(call.call_count, 3)

    def test_three_errors_release_and_resume_never_resets_budget(self):
        call = Mock(side_effect=TimeoutError('timeout'))
        state = self.review(plan([0, 1]), call)
        self.assertEqual(call.call_count, 3)
        self.assertEqual(state['released'], 'limit_released')
        again = Mock(side_effect=AssertionError('must not call'))
        resumed = self.review(state['plan'], again)
        self.assertEqual(len(resumed['attempts']), 3)
        again.assert_not_called()

    def test_third_rejection_does_not_repair_or_make_fourth_call(self):
        issue = self.issue(1, 'repair')
        call = Mock(side_effect=[issue, plan([0, 1]), issue, plan([0, 1]), issue])
        state = self.review(plan([0, 1]), call)
        self.assertEqual(call.call_count, 5)
        self.assertEqual(state['released'], 'limit_released')
        self.assertEqual(len(state['attempts']), 3)

    def test_render_approval_invalidates_changed_plan_and_time_ranges(self):
        state = self.review(plan([0, 1]), Mock(return_value={'passed': True, 'issues': []}))
        require_release(state['plan'], self.tmp.name, self.candidates)
        changed = copy.deepcopy(state['plan'])
        changed['main_product'] = '其他商品'
        with self.assertRaises(AIReturnError):
            require_release(changed, self.tmp.name, self.candidates)
        self.candidates[0]['end'] += .1
        with self.assertRaises(AIReturnError):
            require_release(state['plan'], self.tmp.name, self.candidates)

    def test_interrupted_attempt_is_counted_before_next_review(self):
        data = self.validate(plan([0, 1]))
        save(Path(self.tmp.name) / 'review.json', {
            'source_hash': digest({'candidates': self.candidates, 'labels': self.labels, 'target': (10, 20)}),
            'candidate_hash': digest(self.candidates), 'plan': data, 'excluded': [],
            'attempts': [{'number': 1}, {'number': 2}],
        })
        call = Mock(side_effect=TimeoutError())
        state = self.review(data, call)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(state['released'], 'limit_released')

    def test_existing_completed_jobs_migrate_without_losing_artifacts(self):
        path = Path(self.tmp.name) / 'jobs.db'
        store = Store(path)
        job_id = store.create_job(title='旧成片', source_path='x', workspace='x')
        store.update_job(job_id, status='completed')
        with store.connect() as con:
            con.execute("DELETE FROM stages WHERE stage_id='review'")
        migrated = Store(path).get_job(job_id)
        review = next(s for s in migrated['stages'] if s['stage_id'] == 'review')
        self.assertEqual(review['status'], 'succeeded')
        self.assertEqual(migrated['status'], 'completed')


if __name__ == '__main__':
    unittest.main()
