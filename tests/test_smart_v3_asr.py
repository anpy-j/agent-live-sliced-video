import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_video.smart_v3 import pipeline


class ASRTailTests(unittest.TestCase):
    def understand(self, words, root):
        model = SimpleNamespace(transcribe=lambda *a, **k: ([SimpleNamespace(words=words)], None))
        module = SimpleNamespace(WhisperModel=lambda *a, **k: model)
        with patch.dict('sys.modules', {'faster_whisper': module}), patch.object(pipeline, 'command', return_value='10'):
            return pipeline.understand('unused.wav', root)

    def test_complete_sentences_survive_incomplete_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            words = [SimpleNamespace(start=0, end=2, word='这件外套很好。'),
                     SimpleNamespace(start=3, end=4, word='但是这个')]
            units = self.understand(words, root)
            self.assertEqual([u['text'] for u in units], ['这件外套很好。'])
            self.assertEqual(json.loads((root / 'asr-rejected-tail.json').read_text(encoding='utf-8'))['text'], '但是这个')
            self.assertEqual(len(json.loads((root / 'asr-complete-sentences.json').read_text(encoding='utf-8'))), 1)

    def test_only_incomplete_speech_still_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, 'No complete'):
                self.understand([SimpleNamespace(start=0, end=2, word='没有结束')], Path(tmp))

    def test_imported_incomplete_transcript_stays_strict(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / 'input.json'
            transcript.write_text(json.dumps([{'start': 0, 'end': 2, 'text': '未完成'}]), encoding='utf-8')
            with patch.object(pipeline, 'command', return_value='10'), self.assertRaisesRegex(ValueError, 'punctuated'):
                pipeline.understand('unused.wav', root, transcript)
