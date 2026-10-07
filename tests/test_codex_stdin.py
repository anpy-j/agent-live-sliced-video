import json
import hashlib
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_video.ai import CodexCli


class CodexStdinTest(unittest.TestCase):
    def test_large_chinese_prompt_uses_stdin_for_all_entry_points(self):
        prompt = '挚爱MAX面料、颜色与穿搭。\n' * 10000
        provider = CodexCli(Path('C:/codex.exe'))
        cases = [
            ('generate_json', {'schema': {'type': 'object', 'required': ['decisions']}},
             {'decisions': [{'id': 1, 'usable': True}]}),
            ('generate_plan', {}, {'main_product': '毛衣', 'picks': []}),
            ('generate_visual_plan', {'images': [Path('test.png')]}, {'replacements': []}),
        ]
        for method, kwargs, result in cases:
            with self.subTest(method=method):
                def complete(command, **options):
                    self.assertEqual(command[-1], '-')
                    self.assertNotIn(prompt, command)
                    self.assertEqual(options['stdin_text'], prompt)
                    self.assertLess(len(subprocess.list2cmdline(command)), 4000)
                    self.assertEqual(command[command.index('--model') + 1], 'gpt-5.6-sol')
                    output = Path(command[command.index('--output-last-message') + 1])
                    output.write_text(json.dumps(result, ensure_ascii=False), encoding='utf8')
                    return '', '', .01
                with tempfile.TemporaryDirectory() as tmp, \
                        patch.object(provider, '_ensure_available'), \
                        patch.object(provider, '_complete', side_effect=complete):
                    response = getattr(provider, method)(model='gpt-5.6-sol', prompt=prompt,
                                                         cwd=Path(tmp), **kwargs)
                self.assertIn('data' if method == 'generate_json' else 'plan', response)

    def test_utf8_pipe_transports_the_entire_prompt(self):
        prompt = '中文口播\n' * 10000
        process = Mock(returncode=0)
        process.communicate.return_value = ('{}', '')
        provider = CodexCli(Path('C:/codex.exe'))
        with patch('agent_video.ai.subprocess.Popen', return_value=process) as popen:
            provider._complete(['codex', 'exec', '-'], cwd=Path('.'), on_process=None,
                               timeout=10, started=time.monotonic(), stdin_text=prompt)
        self.assertEqual(popen.call_args.kwargs['stdin'], subprocess.PIPE)
        self.assertEqual(popen.call_args.kwargs['encoding'], 'utf-8')
        process.communicate.assert_called_once_with(input=prompt, timeout=10)

    def test_real_process_receives_long_utf8_input_without_large_argv(self):
        prompt = '挚爱MAX10041005虚拟时间线\n' * 10000
        provider = CodexCli(Path('C:/codex.exe'))
        command = [sys.executable, '-c',
                   "import sys,hashlib; sys.stdin.reconfigure(encoding='utf-8'); "
                   "print(hashlib.sha256(sys.stdin.read().encode('utf-8')).hexdigest())"]
        stdout, stderr, _ = provider._complete(command, cwd=Path('.'), on_process=None,
                                               timeout=10, started=time.monotonic(), stdin_text=prompt)
        self.assertEqual(stdout.strip(), hashlib.sha256(prompt.encode('utf8')).hexdigest())
        self.assertFalse(stderr)


if __name__ == '__main__':
    unittest.main()
