"""watch.py のうち、ネットワークを使わない部分の試験。

実行: python3 -m unittest discover -s skills/model-watch/scripts
"""

import unittest
from datetime import date

from watch import decide, diff_litellm, is_recent, looks_like_model_news, norm, parse_log, parse_toml

TODAY = date(2026, 11, 10)


def result(**kw):
    base = {"new_models": [], "new_labs": [], "deprecations": [], "price_changes": [], "hn": [], "hatena": [], "errors": []}
    return {**base, **kw}


def model(recent, bulk=False):
    return {"id": "lab/m", "recent": recent, "bulk": bulk}


class Norm(unittest.TestCase):
    def test_same_model_in_different_spellings(self):
        self.assertEqual(norm("claude-fable-5.2"), norm("claude-fable-5-2"))
        self.assertEqual(norm("inkling-2-small:free"), "inkling-2-small")

    def test_host_file_contains_model(self):
        self.assertIn(norm("claude-fable-5-2"), norm("global.anthropic.claude-fable-5-2"))


class ParseLog(unittest.TestCase):
    def test_commits_and_paths(self):
        text = ("@2026-11-09T13:40:43-06:00\tfeat(anthropic): add Claude Fable 5.2 (#13204)\n"
                "models/anthropic/claude-fable-5-2.toml\n"
                "providers/anthropic/models/claude-fable-5-2.toml\n"
                "\n"
                "@2026-11-09T20:57:16+08:00\tadd LongCat\n"
                "models/meituan/longcat-2.5.toml\n")
        commits = parse_log(text)
        self.assertEqual(len(commits), 2)
        self.assertEqual(commits[0][1], "feat(anthropic): add Claude Fable 5.2 (#13204)")
        self.assertEqual(commits[0][2], ["models/anthropic/claude-fable-5-2.toml",
                                         "providers/anthropic/models/claude-fable-5-2.toml"])
        self.assertEqual(commits[1][2], ["models/meituan/longcat-2.5.toml"])

    def test_empty(self):
        self.assertEqual(parse_log(""), [])


class ParseToml(unittest.TestCase):
    def test_fields(self):
        t = parse_toml('name = "Claude Fable 5.2"\nrelease_date = "2026-11-09"\nopen_weights = false\n'
                       '[limit]\ncontext = 1_000_000\n')
        self.assertEqual(t["name"], "Claude Fable 5.2")
        self.assertEqual(t["limit"]["context"], 1000000)

    def test_broken_toml_falls_back(self):
        t = parse_toml('name = "X"\nrelease_date = "2026-11-01"\nthis is not toml\n')
        self.assertEqual(t, {"name": "X", "release_date": "2026-11-01"})


class Recent(unittest.TestCase):
    def test_window(self):
        self.assertTrue(is_recent("2026-11-09", TODAY))
        self.assertTrue(is_recent("2026-10-11", TODAY))  # 30 日前
        self.assertFalse(is_recent("2026-10-10", TODAY))
        self.assertFalse(is_recent("2024-03-01", TODAY))  # 過去分のまとめ登録

    def test_month_only(self):
        self.assertTrue(is_recent("2026-11", TODAY))
        self.assertFalse(is_recent("2026-07", TODAY))

    def test_unknown(self):
        self.assertIsNone(is_recent(None, TODAY))
        self.assertIsNone(is_recent("soon", TODAY))


class ModelNews(unittest.TestCase):
    def test_unknown_names_are_kept(self):
        # 提供元の名前では絞らない
        self.assertTrue(looks_like_model_news("Introducing System One Models and Jev"))
        self.assertTrue(looks_like_model_news("Show HN: Open weights for a 4B multimodal model"))
        self.assertTrue(looks_like_model_news("Acme launches a new AI for contracts"))

    def test_unrelated(self):
        self.assertFalse(looks_like_model_news("Postgres 19 released"))
        self.assertFalse(looks_like_model_news("Why I left my job"))


class DiffLitellm(unittest.TestCase):
    def test_new_deprecation_and_price(self):
        before = {"sample_spec": {"input_cost_per_token": 0},
                  "a": {"litellm_provider": "openai", "input_cost_per_token": 1e-06, "output_cost_per_token": 2e-06},
                  "b": {"litellm_provider": "xai", "input_cost_per_token": 3e-06, "output_cost_per_token": 6e-06},
                  "c": {"litellm_provider": "anthropic", "input_cost_per_token": 3e-06, "output_cost_per_token": 6e-06}}
        after = {"sample_spec": {"input_cost_per_token": 1},
                 "a": {"litellm_provider": "openai", "input_cost_per_token": 1e-06, "output_cost_per_token": 2e-06,
                       "deprecation_date": "2027-01-15"},
                 "b": {"litellm_provider": "xai", "input_cost_per_token": 2e-06, "output_cost_per_token": 6e-06},
                 "c": {"litellm_provider": "anthropic", "input_cost_per_token": 3e-06, "output_cost_per_token": 6e-06},
                 "d": {"litellm_provider": "anthropic", "mode": "chat", "input_cost_per_token": 8e-06,
                       "output_cost_per_token": 4e-05}}
        new, deprecations, prices = diff_litellm(before, after)
        self.assertEqual(new, [{"key": "d", "provider": "anthropic", "mode": "chat", "input": 8.0, "output": 40.0}])
        self.assertEqual(deprecations, [{"key": "a", "provider": "openai", "before": None, "after": "2027-01-15"}])
        self.assertEqual(prices, [{"key": "b", "provider": "xai", "input_before": 3.0, "input_after": 2.0,
                                   "output_before": 6.0, "output_after": 6.0}])

    def test_no_change(self):
        same = {"a": {"litellm_provider": "openai", "input_cost_per_token": 1e-06}}
        self.assertEqual(diff_litellm(same, same), ([], [], []))


class Decide(unittest.TestCase):
    def test_nothing(self):
        self.assertFalse(decide(result())["proceed"])

    def test_recent_model(self):
        self.assertTrue(decide(result(new_models=[model(True)]))["proceed"])

    def test_old_backfill_is_skipped(self):
        self.assertFalse(decide(result(new_models=[model(False, bulk=True), model(None, bulk=True)]))["proceed"])

    def test_undated_single_addition(self):
        self.assertTrue(decide(result(new_models=[model(None)]))["proceed"])

    def test_other_signals(self):
        self.assertTrue(decide(result(new_labs=[{"lab": "typesafe"}]))["proceed"])
        self.assertTrue(decide(result(deprecations=[{}]))["proceed"])
        self.assertTrue(decide(result(price_changes=[{}]))["proceed"])
        self.assertTrue(decide(result(hn=[{}]))["proceed"])

    def test_all_sources_failed(self):
        errors = ["models.dev: x", "litellm: x", "hn: x", "hatena: x"]
        self.assertTrue(decide(result(errors=errors))["proceed"])

    def test_partial_failure_alone_is_quiet(self):
        self.assertFalse(decide(result(errors=["litellm: x"]))["proceed"])


if __name__ == "__main__":
    unittest.main()
