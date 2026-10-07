import json
import tempfile
import threading
import unittest

from agent_video.pipeline.order_runtime import OrderRuntime
from agent_video.pipeline.errors import AIReturnError


class OrderRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rows = [{'id': n, 'text': str(n), 'start': n * 5., 'end': n * 5. + 4}
                     for n in range(81)]
        self.calls = []

    def call(self, model, prompt, schema, timeout):
        self.calls.append((schema, timeout))
        if 'groups' in schema['properties']:
            keys = json.loads(prompt.split('\n')[-1])
            return {'groups': [{'keys': keys}]}
        rows = json.loads(prompt.split('候选JSON：\n')[1].split('\n上下文JSON：')[0])
        return {'candidates': [{'id': row['id'], 'topic': 'fabric',
                'facts': ['纯棉' if row['id'] < 40 else '百分百棉'], 'eligible': True,
                'requires': [39] if row['id'] == 40 else [],
                'subject': '本品', 'reason': '材质'} for row in rows]}

    def runtime(self, **kwargs):
        return OrderRuntime(self.tmp.name, 'mock', 900, self.call, **kwargs)

    def test_global_merge_boundary_dependency_and_whole_cache(self):
        labels = self.runtime().labels(self.rows)
        self.assertEqual(labels[40]['requires'], [39])
        self.assertEqual(labels[0]['facts'], labels[80]['facts'])
        self.assertEqual(len(self.calls), 4)
        self.runtime().labels(self.rows)
        self.assertEqual(len(self.calls), 4)
        self.assertTrue(all(timeout <= 90 for _, timeout in self.calls))

    def test_concurrent_batches(self):
        barrier = threading.Barrier(3, timeout=5)
        original = self.call
        def concurrent(*args):
            if 'candidates' in args[2]['properties']:
                barrier.wait()
            return original(*args)
        runtime = self.runtime()
        runtime.call = concurrent
        self.assertEqual(len(runtime.labels(self.rows)), 81)

    def test_partial_cache_survives_failed_global_merge(self):
        runtime = self.runtime()
        original = self.call
        def broken(*args):
            if 'groups' in args[2]['properties']:
                return {'groups': []}
            return original(*args)
        runtime.call = broken
        with self.assertRaises(AIReturnError):
            runtime.labels(self.rows)
        self.assertEqual(len(self.calls), 3)
        self.runtime().labels(self.rows)
        self.assertEqual(len(self.calls), 4)

    def test_budget_exhaustion_starts_no_call(self):
        runtime = self.runtime()
        runtime.deadline = 0
        with self.assertRaisesRegex(AIReturnError, '总时间预算'):
            runtime.labels(self.rows[:1])
        self.assertEqual(self.calls, [])

    def test_cache_invalidated_by_text_and_model(self):
        self.runtime().labels(self.rows[:1])
        changed = [dict(self.rows[0], text='新的事实')]
        self.runtime().labels(changed)
        OrderRuntime(self.tmp.name, 'other', 900, self.call).labels(changed)
        self.assertEqual(len(self.calls), 3)

    def test_global_reconcile_rejects_duplicate_and_missing_keys(self):
        original = self.call
        def invalid(*args):
            if 'groups' in args[2]['properties']:
                return {'groups': [{'keys': ['纯棉', '纯棉']}]}
            return original(*args)
        runtime = self.runtime()
        runtime.call = invalid
        with self.assertRaises(AIReturnError):
            runtime.labels(self.rows)


if __name__ == '__main__':
    unittest.main()
