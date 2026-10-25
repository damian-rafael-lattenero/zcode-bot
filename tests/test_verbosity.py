"""F61 — /verbosity: ONE level for how much a turn streams.

The pure layer (presets, refresh, born_open), the born-open freeze format
(box_open_view) and the handler/callback plumbing.
"""
import os
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))

import support  # noqa: E402
from zcode_bot import verbosity as vb  # noqa: E402
from zcode_bot import reasoning  # noqa: E402
from zcode_bot.commands import callbacks as cb  # noqa: E402
from zcode_bot.commands import handlers_system as hs  # noqa: E402

SID = "sess_8316d9b7-c0f0-49cc-a221-af3268d2a8f0"

# the "normal" preset IS the production default — pinned literally (the
# suite's support.load_module mutates config.DEFAULTS, so comparing
# against them would test the fixture, not the invariant)
NORMAL = {"cot_stream": True, "cot_buttons": True, "cot_empty_notice": True,
          "tools_stream": True, "tools_box_filter": "action",
          "live_progress": True, "mac_spectator": True,
          "mac_cot_buttons": True}


class TestPresets(unittest.TestCase):
    def test_normal_is_the_default(self):
        self.assertEqual(vb._LEVEL_KNOBS["normal"], NORMAL)

    def test_summary_mutes_every_stream_but_the_live_box(self):
        self.assertEqual(vb._LEVEL_KNOBS["summary"], {
            "cot_stream": False, "cot_buttons": False,
            "cot_empty_notice": False, "tools_stream": False,
            "tools_box_filter": "minimal", "live_progress": True,
            "mac_spectator": False, "mac_cot_buttons": False})

    def test_verbose_shows_every_tool(self):
        self.assertEqual(vb._LEVEL_KNOBS["verbose"],
                         dict(NORMAL, tools_box_filter="all"))

    def test_apply_level_sets_level_and_knobs_in_place(self):
        cfg = dict(NORMAL)
        out = vb.apply_level(cfg, "summary")
        self.assertIs(out, cfg)
        self.assertEqual(cfg["verbosity"], "summary")
        self.assertFalse(cfg["cot_buttons"])
        self.assertFalse(cfg["mac_spectator"])

    def test_apply_level_unknown_raises(self):
        with self.assertRaises(ValueError):
            vb.apply_level({}, "loud")

    def test_level_of_unknown_reads_as_normal(self):
        self.assertEqual(vb.level_of({}), "normal")
        self.assertEqual(vb.level_of({"verbosity": "LOUD"}), "normal")

    def test_born_open_only_verbose(self):
        self.assertFalse(vb.born_open({}))
        self.assertFalse(vb.born_open({"verbosity": "summary"}))
        self.assertTrue(vb.born_open({"verbosity": "verbose"}))


class TestRefresh(unittest.TestCase):
    """The WARM refresh: a long-lived worker re-reads the level's keys —
    /verbosity applies on the NEXT turn, not on the next process."""

    def test_refresh_copies_the_levels_keys_only(self):
        cfg = dict(NORMAL, verbosity="normal")
        disk = dict(NORMAL, verbosity="summary", cot_buttons=False,
                    token="other-token", chat_id=99)
        vb.refresh(cfg, disk)
        self.assertEqual(cfg["verbosity"], "summary")
        self.assertFalse(cfg["cot_buttons"])
        self.assertNotIn("token", cfg)  # transport keys are not copied

    def test_refresh_without_disk_argument_reads_config(self):
        self.zt = support.fresh(self)
        from zcode_bot.utils.config import save_config, load_config
        save_config(dict(vb._LEVEL_KNOBS["normal"], verbosity="verbose",
                         enabled=True, token="T", chat_id=1))
        cfg = dict(NORMAL, verbosity="normal")
        vb.refresh(cfg)   # disk=None → load_config()
        self.assertEqual(cfg["verbosity"], "verbose")


class TestBornOpenView(unittest.TestCase):
    """box_open_view — the verbose freeze: show()'s layout + ⤴️."""

    def test_small_box_one_part_with_collapse(self):
        text, kb = reasoning.box_open_view(SID, 1760000000, 3, "CoT 2.1",
                                           "title", "plain reasoning")
        self.assertIn("<b>CoT 2.1</b>", text)
        self.assertIn("plain reasoning", text)
        rows = kb["inline_keyboard"]
        self.assertEqual(rows[-1][0]["callback_data"],
                         "cr:%s:1760000000:3" % SID)

    def test_big_box_opens_at_part_1_with_parts_row(self):
        # raw text at the cap BUT esc-inflating: the part split happens
        # on the ESCAPED text, so it lands past one part
        big = "<&>" * (reasoning.COT_BOX_CAP // 3 + 10)
        text, kb = reasoning.box_open_view(SID, 1760000000, 1, "CoT 1.1",
                                           "t", big)
        self.assertIn("part 1/", text)
        rows = kb["inline_keyboard"]
        self.assertEqual(rows[-1][0]["text"], "⤴️ Collapse")  # under parts
        self.assertTrue(rows[0][0]["callback_data"].startswith("ct:%s:" % SID))


class TestHandlerAndCallback(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.saved = support.patch(self, hs, "save_config",
                                   lambda cfg: None)
        self.base = dict(NORMAL, verbosity="normal")

    def test_no_arg_shows_three_buttons_current_marked(self):
        out = hs._cmd_verbosity(dict(self.base), "")
        self.assertIsInstance(out, tuple)
        text, kb = out
        row = kb["inline_keyboard"][0]
        self.assertEqual([b["callback_data"] for b in row],
                         ["vb:summary", "vb:normal", "vb:verbose"])
        self.assertEqual([b["text"] for b in row][1], "● normal")

    def test_setting_a_level_saves_and_confirms(self):
        cfg = dict(self.base)
        out = hs._cmd_verbosity(cfg, "verbose")
        self.assertIn("verbose", out)
        self.assertIn("applies on the next turn", out)
        # the preset was applied to the cfg the handler saves whole
        self.assertEqual(cfg["tools_box_filter"], "all")
        self.assertEqual(cfg["verbosity"], "verbose")

    def test_unknown_level_lists_the_levels(self):
        out = hs._cmd_verbosity(dict(self.base), "loud")
        self.assertIn("summary · normal · verbose", out)

    def test_callback_translates_the_preset_buttons(self):
        self.assertEqual(cb.callback_text("vb:summary"), "/verbosity summary")


if __name__ == "__main__":
    unittest.main()
