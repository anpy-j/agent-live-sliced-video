import json
import tempfile
import threading
import unittest
from unittest.mock import patch

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
        self.assertTrue(all(timeout == 900 for _, timeout in self.calls))

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

    def test_each_call_receives_full_timeout_and_ignores_old_stage_budget(self):
        with patch.dict('os.environ', {'PIPELINE_S4_BUDGET': '1'}):
            runtime = self.runtime()
            runtime.labels(self.rows[:1])
            runtime.invoke('mock', '候选JSON：\n' + json.dumps(self.rows[:1]),
                           {'properties': {'candidates': {}}}, 900)
        self.assertEqual([timeout for _, timeout in self.calls], [900, 900])

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

    def test_workbuddy_splits_only_uncached_batches(self):
        with patch.dict('os.environ', {'PIPELINE_AI_PROVIDER': 'workbuddy'}):
            runtime = self.runtime()
            old = self.call('mock', '候选JSON：\n' + json.dumps(self.rows[:40]),
                            {'properties': {'candidates': {}}}, 90)
            from agent_video.pipeline.editorial import save, digest
            key = digest(dict(runtime.identity, rows=self.rows[:40], context=self.rows[40:43]))
            save(runtime.root / (key + '.json'), old)
            self.calls.clear()
            counts = []
            original = self.call
            def record(*args):
                if 'candidates' in args[2]['properties']:
                    counts.append(len(json.loads(args[1].split('候选JSON：\n')[1].split('\n上下文JSON：')[0])))
                return original(*args)
            runtime.call = record
            self.assertEqual(len(runtime.labels(self.rows)), 81)
            self.assertEqual(sorted(counts), [1, 20, 20])

    def test_all_batches_run_without_elapsed_time_degradation(self):
        from concurrent.futures import ThreadPoolExecutor as Real
        runtime = self.runtime()
        original = self.call
        calls = []
        def record(*args):
            if 'candidates' in args[2]['properties']:
                calls.append(args)
            return original(*args)
        runtime.call = record
        with patch('agent_video.pipeline.order_runtime.ThreadPoolExecutor',
                   side_effect=lambda **kwargs: Real(max_workers=1)):
            labels = runtime.labels(self.rows)
        self.assertEqual(len(labels), 81)
        self.assertEqual(len(calls), 3)
        self.assertIn(80, labels)

    def test_workers_scale_with_batch_count(self):
        from concurrent.futures import ThreadPoolExecutor as Real
        sizes = []
        def capture(**kwargs):
            sizes.append(kwargs['max_workers'])
            return Real(**kwargs)
        def run(rows):
            sizes.clear()
            with patch('agent_video.pipeline.order_runtime.ThreadPoolExecutor',
                       side_effect=capture):
                self.runtime().labels(rows)
            return sizes[0]
        def rows(count):
            return [{'id': n, 'text': str(n), 'start': n * 5., 'end': n * 5. + 4}
                    for n in range(count)]
        self.assertEqual(run(self.rows), 3)
        self.assertEqual(run(rows(600)), 6)

    def test_failed_worker_does_not_start_queued_model_calls(self):
        from concurrent.futures import ThreadPoolExecutor
        runtime = self.runtime()
        calls = []
        def fail(*args):
            calls.append(args)
            raise AIReturnError('original timeout')
        runtime.call = fail
        with patch('agent_video.pipeline.order_runtime.ThreadPoolExecutor',
                   side_effect=lambda **kwargs: ThreadPoolExecutor(max_workers=1)):
            with self.assertRaisesRegex(AIReturnError, 'original timeout'):
                runtime.labels(self.rows)
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
