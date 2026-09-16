import unittest

from agent_video.mcp import McpEndpoint


class McpTest(unittest.TestCase):
    def setUp(self):
        self.endpoint = McpEndpoint(lambda name, args: {"name": name, "args": args})

    def test_initialize(self):
        status, body = self.endpoint.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["serverInfo"]["name"], "agent-live-sliced-video")

    def test_tools_call(self):
        status, body = self.endpoint.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                             "params": {"name": "list_video_jobs", "arguments": {}}})
        self.assertFalse(body["result"]["isError"])
        self.assertEqual(body["result"]["structuredContent"]["name"], "list_video_jobs")


if __name__ == "__main__":
    unittest.main()

