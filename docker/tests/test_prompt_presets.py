"""Tests for the 0.5.0 preset-prompt feature.

A preset stores a long prompt under a short name so anyone in the chat can draw
with ``/jjcp 名字 补充描述`` instead of pasting the whole style block again.
"""

from __future__ import annotations

import asyncio
import base64
import json
import types
import unittest

from tests.test_arena_image_hardening import (
    PLUGIN_ROOT,
    PNG_MAGIC,
    FakeEvent,
    _collect,
    _make_plugin,
    _plugin_module,
)


def _collect_many(*generators) -> list[list]:
    """Drain several handlers inside one event loop.

    ``_presets_lock`` binds to the first loop that awaits it, so a multi-step
    flow (add, then draw) has to share a single ``asyncio.run``.
    """

    async def runner():
        return [[item async for item in generator] for generator in generators]

    return asyncio.run(runner())


def _texts(results) -> str:
    return "\n".join(result.text for result in results)


class PresetExpansionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.expand = _plugin_module().ArenaImagePlugin._expand_preset

    def test_placeholder_is_where_the_callers_words_land(self) -> None:
        self.assertEqual(
            self.expand("cyberpunk city, {}, rain", "一只猫"),
            "cyberpunk city, 一只猫, rain",
        )

    def test_every_supported_placeholder_spelling_works(self) -> None:
        for placeholder in ("{}", "{prompt}", "{描述}", "{补充}"):
            with self.subTest(placeholder=placeholder):
                self.assertEqual(self.expand(f"水彩，{placeholder}", "猫"), "水彩，猫")

    def test_a_style_only_preset_appends_the_words(self) -> None:
        self.assertEqual(self.expand("水彩风格", "一只猫"), "水彩风格，一只猫")

    def test_no_words_leaves_a_style_preset_untouched(self) -> None:
        self.assertEqual(self.expand("水彩风格", ""), "水彩风格")

    def test_an_empty_placeholder_does_not_leave_dangling_separators(self) -> None:
        self.assertEqual(self.expand("水彩，{}，柔和光线", ""), "水彩，柔和光线")
        self.assertEqual(self.expand("watercolor, {}", ""), "watercolor")
        self.assertEqual(self.expand("{}，水彩", ""), "水彩")


class PresetStorageTest(unittest.TestCase):
    def test_config_lines_seed_the_preset_table(self) -> None:
        _, plugin = _make_plugin(
            self,
            {"preset_prompts": ["赛博=cyberpunk city, {}", "手绘：水彩风格"]},
        )
        self.assertEqual(sorted(plugin._presets), ["手绘", "赛博"])
        self.assertEqual(plugin._presets["赛博"]["prompt"], "cyberpunk city, {}")
        self.assertEqual(plugin._presets["手绘"]["prompt"], "水彩风格")
        self.assertEqual(plugin._presets["赛博"]["source"], "config")

    def test_a_saved_preset_is_never_shadowed_by_the_config(self) -> None:
        config = {"preset_prompts": ["赛博=来自面板"]}
        main, plugin = _make_plugin(self, config)
        plugin._presets["赛博"] = {"prompt": "来自聊天", "model": "", "source": "chat"}
        asyncio.run(plugin._save_presets())
        self.assertEqual(
            json.loads(plugin.presets_file.read_text(encoding="utf-8"))["赛博"]["prompt"],
            "来自聊天",
        )
        reloaded = main.ArenaImagePlugin(context=object(), config=dict(config))
        self.assertEqual(reloaded._presets["赛博"]["prompt"], "来自聊天")

    def test_unusable_rows_are_dropped_instead_of_breaking_startup(self) -> None:
        main, plugin = _make_plugin(self)
        plugin.presets_file.write_text(
            json.dumps(
                {
                    "": "空名字",
                    "名" * (main.MAX_PRESET_NAME_LENGTH + 1): "名字太长",
                    "空提示词": "   ",
                    "类型不对": 5,
                    "好的": {"prompt": "watercolor", "model": "gpt-image-2 (medium)"},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        reloaded = main.ArenaImagePlugin(context=object(), config={})
        self.assertEqual(list(reloaded._presets), ["好的"])
        self.assertEqual(reloaded._presets["好的"]["model"], "gpt-image-2 (medium)")

    def test_a_corrupt_file_falls_back_to_the_config(self) -> None:
        main, plugin = _make_plugin(self)
        plugin.presets_file.write_text("{not json", encoding="utf-8")
        reloaded = main.ArenaImagePlugin(
            context=object(),
            config={"preset_prompts": ["手绘=水彩风格"]},
        )
        self.assertEqual(list(reloaded._presets), ["手绘"])

    def test_a_long_prompt_is_truncated_rather_than_rejected(self) -> None:
        main, _ = _make_plugin(self)
        cleaned = main.ArenaImagePlugin._clean_preset("长", {"prompt": "细节" * 3000})
        self.assertIsNotNone(cleaned)
        self.assertEqual(len(cleaned[1]["prompt"]), main.MAX_PRESET_PROMPT_LENGTH)

    def test_the_shipped_defaults_all_load(self) -> None:
        main, _ = _make_plugin(self)
        schema = json.loads(
            (PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8")
        )
        entry = schema["preset_prompts"]
        self.assertEqual(entry["type"], "list")
        defaults = entry["default"]
        self.assertTrue(defaults)
        plugin = main.ArenaImagePlugin(
            context=object(),
            config={"preset_prompts": list(defaults)},
        )
        self.assertEqual(len(plugin._presets), len(defaults))
        for name, preset in plugin._presets.items():
            with self.subTest(preset=name):
                self.assertTrue(preset["prompt"])
                self.assertTrue(
                    any(mark in preset["prompt"] for mark in main.PRESET_PLACEHOLDERS)
                )


class _Recorder:
    """Stands in for ``_generate`` and remembers what the command asked for."""

    def __init__(self, event) -> None:
        self.event = event
        self.calls: list[dict] = []

    def __call__(self, event, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})

        async def stream():
            yield self.event.plain_result("出图完成")

        return stream()

    @property
    def last(self) -> dict:
        return self.calls[-1]


def _wire(plugin, event, models=("gpt-image-2 (medium)", "mona-lisa")):
    """Give the plugin a fake model table and swallow the actual generation."""

    async def fetch_models(*, force: bool = False):  # noqa: ARG001
        return [{"id": name, "publicName": name} for name in models]

    plugin._fetch_models = fetch_models
    recorder = _Recorder(event)
    plugin._generate = recorder
    plugin._client = lambda: (_ for _ in ()).throw(
        AssertionError("a preset test must not reach the bridge")
    )
    return recorder


class PresetCommandTest(unittest.TestCase):
    def test_add_then_draw_uses_the_stored_prompt(self) -> None:
        _, plugin = _make_plugin(self)
        event = FakeEvent()
        recorder = _wire(plugin, event)
        added, drawn = _collect_many(
            plugin.add_preset(event, "赛博 cyberpunk city, {}, neon"),
            plugin.preset_image(event, "赛博 一只猫"),
        )
        self.assertIn("已添加预设「赛博」", _texts(added))
        self.assertIn("预设「赛博」", _texts(drawn))
        self.assertEqual(recorder.last["prompt"], "cyberpunk city, 一只猫, neon")
        self.assertFalse(recorder.last["include_input_images"])
        self.assertEqual(recorder.last["model_id"], "gpt-image-2 (medium)")

    def test_drawing_without_extra_words_keeps_the_template(self) -> None:
        _, plugin = _make_plugin(self, {"preset_prompts": ["手绘=水彩风格，{}，柔光"]})
        event = FakeEvent()
        recorder = _wire(plugin, event)
        _collect_many(plugin.preset_image(event, "手绘"))
        self.assertEqual(recorder.last["prompt"], "水彩风格，柔光")

    def test_an_unknown_name_lists_what_exists_and_draws_nothing(self) -> None:
        _, plugin = _make_plugin(self, {"preset_prompts": ["手绘=水彩风格"]})
        event = FakeEvent()
        recorder = _wire(plugin, event)
        results = _collect(plugin.preset_image(event, "赛博 一只猫"))
        self.assertEqual(recorder.calls, [])
        self.assertIn("没有预设「赛博」", results[0].text)
        self.assertIn("手绘", results[0].text)

    def test_the_name_is_matched_case_insensitively(self) -> None:
        _, plugin = _make_plugin(self, {"preset_prompts": ["Cyber=neon, {}"]})
        event = FakeEvent()
        recorder = _wire(plugin, event)
        _collect_many(plugin.preset_image(event, "cyber 猫"))
        self.assertEqual(recorder.last["prompt"], "neon, 猫")

    def test_an_attached_picture_turns_a_preset_into_image_to_image(self) -> None:
        main, plugin = _make_plugin(self, {"preset_prompts": ["手绘=水彩风格，{}"]})
        event = FakeEvent()
        event.message_obj = types.SimpleNamespace(
            message=[
                main.Image(
                    file="/tmp/ref.png",
                    base64_value=base64.b64encode(PNG_MAGIC + b"ref").decode("ascii"),
                )
            ]
        )
        recorder = _wire(plugin, event)
        results = _collect_many(plugin.preset_image(event, "手绘 猫"))[0]
        self.assertIn("开始图生图", _texts(results))
        self.assertTrue(recorder.last["include_input_images"])
        self.assertEqual(len(recorder.last["input_images"]), 1)

    def test_a_pinned_model_beats_the_current_selection(self) -> None:
        _, plugin = _make_plugin(self, {"preset_prompts": ["手绘=水彩，{}"]})
        event = FakeEvent()
        recorder = _wire(plugin, event)
        bound, drawn = _collect_many(
            plugin.bind_preset_model(event, "手绘 2"),
            plugin.preset_image(event, "手绘 猫"),
        )
        self.assertIn("已固定到模型：mona-lisa", _texts(bound))
        self.assertIn("（预设固定）", _texts(drawn))
        self.assertEqual(recorder.last["model_id"], "mona-lisa")

    def test_the_pin_can_be_released_back_to_the_current_model(self) -> None:
        _, plugin = _make_plugin(self, {"preset_prompts": ["手绘=水彩，{}"]})
        event = FakeEvent()
        recorder = _wire(plugin, event)
        _, released, drawn = _collect_many(
            plugin.bind_preset_model(event, "手绘 2"),
            plugin.bind_preset_model(event, "手绘 无"),
            plugin.preset_image(event, "手绘 猫"),
        )
        self.assertIn("已解绑固定模型", _texts(released))
        self.assertNotIn("（预设固定）", _texts(drawn))
        self.assertEqual(recorder.last["model_id"], "gpt-image-2 (medium)")

    def test_rewriting_a_preset_keeps_its_pinned_model(self) -> None:
        _, plugin = _make_plugin(self, {"preset_prompts": ["手绘=水彩，{}"]})
        event = FakeEvent()
        _wire(plugin, event)
        _, updated = _collect_many(
            plugin.bind_preset_model(event, "手绘 2"),
            plugin.add_preset(event, "手绘 新的水彩提示词，{}"),
        )
        self.assertIn("已更新预设「手绘」", _texts(updated))
        self.assertEqual(plugin._presets["手绘"]["model"], "mona-lisa")
        self.assertEqual(plugin._presets["手绘"]["prompt"], "新的水彩提示词，{}")
        self.assertEqual(len(plugin._presets), 1)

    def test_delete_removes_it_from_disk_too(self) -> None:
        _, plugin = _make_plugin(self)
        event = FakeEvent()
        _wire(plugin, event)
        _, deleted, drawn = _collect_many(
            plugin.add_preset(event, "手绘 水彩"),
            plugin.delete_preset(event, "手绘"),
            plugin.preset_image(event, "手绘 猫"),
        )
        self.assertIn("已删除预设「手绘」", _texts(deleted))
        self.assertEqual(plugin._presets, {})
        self.assertEqual(json.loads(plugin.presets_file.read_text(encoding="utf-8")), {})
        self.assertIn("没有预设「手绘」", _texts(drawn))

    def test_the_cap_stops_unbounded_growth(self) -> None:
        main, plugin = _make_plugin(self)
        event = FakeEvent()
        _wire(plugin, event)
        plugin._presets = {
            f"p{index}": {"prompt": "x", "model": "", "source": "chat"}
            for index in range(main.MAX_PRESETS)
        }
        results = _collect(plugin.add_preset(event, "再来一条 提示词"))
        self.assertIn(f"上限 {main.MAX_PRESETS}", results[0].text)
        self.assertEqual(len(plugin._presets), main.MAX_PRESETS)

    def test_a_rewrite_is_allowed_even_at_the_cap(self) -> None:
        main, plugin = _make_plugin(self)
        event = FakeEvent()
        _wire(plugin, event)
        plugin._presets = {
            f"p{index}": {"prompt": "x", "model": "", "source": "chat"}
            for index in range(main.MAX_PRESETS)
        }
        results = _collect_many(plugin.add_preset(event, "p0 新的提示词"))[0]
        self.assertIn("已更新预设「p0」", _texts(results))
        self.assertEqual(plugin._presets["p0"]["prompt"], "新的提示词")
        self.assertEqual(len(plugin._presets), main.MAX_PRESETS)

    def test_the_list_shows_names_pins_and_a_short_preview(self) -> None:
        _, plugin = _make_plugin(
            self,
            {"preset_prompts": ["手绘=" + "水彩" * 80, "赛博=neon"]},
        )
        plugin._presets["赛博"]["model"] = "mona-lisa"
        text = _collect(plugin.list_presets(FakeEvent()))[0].text
        self.assertIn("预设提示词（2 条）", text)
        self.assertIn("· 手绘", text)
        self.assertIn("· 赛博（固定模型：mona-lisa）", text)
        self.assertIn("…", text)
        self.assertIn("/jjcp", text)
        self.assertLess(len(text), 400)

    def test_a_long_list_is_truncated(self) -> None:
        main, plugin = _make_plugin(self)
        plugin._presets = {
            f"p{index}": {"prompt": "x", "model": "", "source": "chat"}
            for index in range(main.PRESET_LIST_LIMIT + 5)
        }
        text = _collect(plugin.list_presets(FakeEvent()))[0].text
        self.assertIn("其余 5 条已省略", text)
        self.assertNotIn(f"· p{main.PRESET_LIST_LIMIT}\n", text)

    def test_an_empty_table_explains_how_to_add_one(self) -> None:
        _, plugin = _make_plugin(self)
        text = _collect(plugin.list_presets(FakeEvent()))[0].text
        self.assertIn("/竞技场预设添加", text)

    def test_usage_hints_are_shown_instead_of_silent_no_ops(self) -> None:
        _, plugin = _make_plugin(self)
        event = FakeEvent()
        _wire(plugin, event)
        for handler, argument, expected in (
            (plugin.preset_image, "", "用法：/jjcp"),
            (plugin.add_preset, "只有名字", "用法：/竞技场预设添加"),
            (plugin.delete_preset, "", "用法：/竞技场预设删除"),
            (plugin.bind_preset_model, "手绘", "用法：/竞技场预设模型"),
        ):
            with self.subTest(expected=expected):
                results = _collect(handler(event, argument))
                self.assertEqual(len(results), 1)
                self.assertIn(expected, results[0].text)


class PresetPermissionTest(unittest.TestCase):
    def test_writing_commands_require_admin(self) -> None:
        main = _plugin_module()
        for name in ("add_preset", "delete_preset", "bind_preset_model"):
            with self.subTest(command=name):
                handler = getattr(main.ArenaImagePlugin, name)
                self.assertEqual(
                    getattr(handler, "arena_permission", None),
                    main.filter.PermissionType.ADMIN,
                )

    def test_reading_and_drawing_stay_open_to_members(self) -> None:
        main = _plugin_module()
        for name in ("list_presets", "preset_image"):
            with self.subTest(command=name):
                handler = getattr(main.ArenaImagePlugin, name)
                self.assertIsNone(getattr(handler, "arena_permission", None))

    def test_the_documented_command_names_are_the_registered_ones(self) -> None:
        main = _plugin_module()
        expected = {
            "preset_image": ("jjcp", {"竞技场预设画图", "预设画图", "jjc预设"}),
            "list_presets": ("竞技场预设", {"arena预设", "竞技场预设列表"}),
            "add_preset": ("竞技场预设添加", {"arena预设添加", "竞技场添加预设"}),
            "delete_preset": ("竞技场预设删除", {"arena预设删除", "竞技场删除预设"}),
            "bind_preset_model": ("竞技场预设模型", {"arena预设模型", "竞技场预设绑定模型"}),
        }
        for name, (command, aliases) in expected.items():
            with self.subTest(command=command):
                handler = getattr(main.ArenaImagePlugin, name)
                self.assertEqual(handler.arena_command, command)
                self.assertEqual(handler.arena_aliases, aliases)


if __name__ == "__main__":
    unittest.main()
