"""F54b — the on-disk schemas are executable documentation: the
required-key sets are pinned here and the entity to_dict() outputs are
checked against them, so key drift fails the suite instead of aging
silently on disk."""
import tempfile
import unittest

from zcode_bot import schemas
from zcode_bot.nuclear import NuclearReport
from zcode_bot.state.registry import QuestionFiles
from zcode_bot.workers import QueueItem, WorkerEntry


class TestSchemasPinned(unittest.TestCase):
    def test_required_keys(self):
        self.assertEqual(set(schemas.CotCache.__required_keys__),
                         {"sid", "t0", "title", "boxes", "labels"})
        self.assertEqual(set(schemas.CotCacheExtra.__required_keys__), set())
        self.assertEqual(set(schemas.QueueItemShape.__required_keys__),
                         {"act", "text", "ts"})
        self.assertEqual(set(schemas.QuestionFile.__required_keys__),
                         {"request_id", "questions", "title", "sid", "t0"})
        self.assertEqual(set(schemas.AnswerFile.__required_keys__),
                         {"request_id", "answer"})
        self.assertEqual(set(schemas.NuclearReportShape.__required_keys__),
                         {"old_pid", "t0", "dur", "stopped", "total", "mac",
                          "queued", "alive", "app_ok", "notes"})
        # total=False shapes: everything optional, exact optional set pinned
        self.assertEqual(set(schemas.WorkersEntry.__optional_keys__),
                         {"t0", "prompt", "ws", "title", "pid", "stopped",
                          "waiting"})
        self.assertEqual(set(schemas.ActiveTurn.__optional_keys__),
                         {"title", "t0", "pid", "ws", "last_id", "stop"})
        self.assertEqual(set(schemas.AuditEntry.__optional_keys__),
                         {"ts", "sid", "type", "detail", "key"})
        self.assertEqual(set(schemas.SessionShape.__optional_keys__),
                         {"sid", "title", "ws", "model", "mode", "mode_set"})

    def test_entity_outputs_match(self):
        q = QueueItem(act={"sid": "s"}, text="hello", ts=1.0)
        self.assertEqual(set(q.to_dict()),
                         set(schemas.QueueItemShape.__annotations__))
        w = WorkerEntry(t0=1.0, prompt="p", ws="/w", title="t", pid=5)
        self.assertEqual(set(w.to_dict()),
                         {"t0", "prompt", "ws", "title", "pid"})
        self.assertTrue(set(w.to_dict())
                        <= set(schemas.WorkersEntry.__annotations__))
        n = NuclearReport()
        self.assertEqual(set(n.to_dict()),
                         set(schemas.NuclearReportShape.__annotations__))

    def test_question_and_answer_shapes(self):
        with tempfile.TemporaryDirectory() as d:
            qf = QuestionFiles(d)
            obj = qf.ask("rid1", ["OK?"], "t", sid="s")
            self.assertEqual(set(obj),
                             set(schemas.QuestionFile.__annotations__))
            qf.answer("rid1", "yes")
            import json
            import os
            with open(os.path.join(d, "a-rid1.json")) as f:
                self.assertEqual(set(json.load(f)),
                                 set(schemas.AnswerFile.__annotations__))


if __name__ == "__main__":
    unittest.main()
