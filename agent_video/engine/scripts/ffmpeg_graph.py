# -*- coding: utf-8 -*-
"""ffmpeg helpers shared by the render scripts.

Windows CreateProcess caps a single command line at 32767 chars, and long
timelines inline their whole filter graph into `-filter_complex`, so the
graph travels via a file instead. ffmpeg >= 7 removed the dedicated
`-filter_complex_script` option; the generic `-/filter_complex <file>`
loader replaces it, while older builds only know the dedicated one.
"""
import os
import subprocess
import tempfile


def _write_filter_script(graph):
    descriptor, path = tempfile.mkstemp(prefix="ffmpeg_graph_", suffix=".filter")
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(graph)
    return path


_SCRIPT_OPTION = None


def _script_option():
    global _SCRIPT_OPTION
    if _SCRIPT_OPTION is None:
        probe = _write_filter_script("[0:v]null[v]")
        try:
            result = None
            for option in ("-/filter_complex", "-filter_complex_script"):
                result = subprocess.run(
                    ["ffmpeg", "-v", "error", "-f", "lavfi",
                     "-i", "color=black:s=16x16:d=0.04", option, probe,
                     "-map", "[v]", "-f", "null", "-"],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace")
                if result.returncode == 0:
                    _SCRIPT_OPTION = option
                    break
            if _SCRIPT_OPTION is None:
                raise RuntimeError("ffmpeg 无法从文件加载 filter graph："
                                   + (result.stderr[-300:] if result else ""))
        finally:
            os.unlink(probe)
    return _SCRIPT_OPTION


def filter_complex_args(filters):
    """Return (argv fragment, graph file) for the serialized filter graph.

    The graph file must be deleted after the ffmpeg invocation completes.
    """
    path = _write_filter_script(";".join(filters))
    return [_script_option(), path], path
