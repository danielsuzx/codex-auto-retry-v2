"""离线回归；所有执行路径 mock 输入 API，不向真实聊天发送。"""
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location("codex_auto_retry_v2", ROOT / "codex_auto_retry_v2.py")
retry = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(retry)


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(retry, "RapidOCR", lambda: Mock(return_value=([], None)))
    instance = retry.CodexAutoRetryApp()
    instance.log = Mock()
    return instance


@pytest.fixture(autouse=True)
def no_live_inputs(monkeypatch):
    """测试中遗漏任何真实输入调用都会失败，不能操作用户桌面。"""
    def prohibited(*args, **kwargs):
        raise AssertionError("Live desktop input is prohibited in tests")
    monkeypatch.setattr(retry.u32, "SetCursorPos", Mock(side_effect=prohibited))
    monkeypatch.setattr(retry.u32, "mouse_event", Mock(side_effect=prohibited))
    monkeypatch.setattr(retry.u32, "keybd_event", Mock(side_effect=prohibited))
    monkeypatch.setattr(retry.u32, "SendInput", Mock(side_effect=prohibited))
    monkeypatch.setattr(retry.u32, "SetForegroundWindow", Mock(side_effect=prohibited))
    monkeypatch.setattr(retry.u32, "BringWindowToTop", Mock(side_effect=prohibited))
    monkeypatch.setattr(retry, "user_input_active", lambda: False)


def line(text, y, left=90, right=550, score=0.99, scale=1):
    box = [[left * scale, (y - 10) * scale], [right * scale, (y - 10) * scale],
           [right * scale, (y + 10) * scale], [left * scale, (y + 10) * scale]]
    return [box, text, score]


def scene(lines=(), draft=None, placeholder=True, scale=1):
    result = [line("完全访问", 950, 120, 220, scale=scale),
              line("GPT-6.1 Sol Ultra", 950, 550, 720, scale=scale)]
    if placeholder and draft is None:
        result.append(line("和 Codex 一起工作", 900, 120, 340, scale=scale))
    if draft is not None:
        if isinstance(draft, str):
            draft = [draft]
        result.extend(line(text, 875 + i * 22, 150, 550, scale=scale) for i, text in enumerate(draft))
    result.extend(line(text, y, left, right, score, scale)
                  for text, y, left, right, score in lines)
    return result


def entry(text, y, left=90, right=550, score=0.99):
    return text, y, left, right, score


ERROR = "rate limit exceeded: Your requests have exceeded token rate limit."
PROMPT = entry("继续", 400, 820, 880)


def state(app, lines, **kwargs):
    scale = kwargs.get("scale", 1)
    return app.analyze_frame(scene(lines, **kwargs), 1000 * scale, 1000 * scale, scale)


@pytest.mark.parametrize("scale", [1, 1.5, 2])
def test_only_latest_turn_counts_not_old_command(app, scale):
    st = state(app, [entry("已运行命令 pytest", 180), entry(ERROR, 260), PROMPT,
                     entry("正在重新连接 5/5", 550), entry(ERROR, 650)], scale=scale)
    assert st["progress"] == "none"
    assert st["action"] == "SIT_2"
    assert st["target_prompt"]["text"] == "继续"
    assert st["progress_evidence"] == []


@pytest.mark.parametrize("noise", ["正在重新连接 5/5", "reconnecting 5/5", "已处理 3 分钟", "已处理 3 分 20 秒",
                                    "Thought for 12s", "+23 -7", "撤销", "查看变更", "正在思考"])
def test_status_and_auxiliary_controls_are_not_progress(app, noise):
    st = state(app, [PROMPT, entry(noise, 500), entry(ERROR, 650)])
    assert st["progress"] == "none"
    assert st["action"] == "SIT_2"
    assert not st["busy"]


@pytest.mark.parametrize("progress", ["已运行命令 pytest", "已编辑 foo.py", "已读取 README.md", "Ran command: pytest",
                                       "我已找到问题并修改了重试逻辑。"])
def test_actual_progress_in_current_turn_continues(app, progress):
    st = state(app, [PROMPT, entry(progress, 500), entry("正在重新连接5/5", 550), entry(ERROR, 650)])
    assert st["action"] == "SIT_1"
    assert st["progress"] == "yes"
    assert progress in st["progress_evidence"]


@pytest.mark.parametrize("prompt", ["继续", "重试", "Retry", "Continue", "请修复这个异常并补充测试"])
def test_prompt_not_limited_to_resume_keywords(app, prompt):
    st = state(app, [entry(prompt, 350, 620, 910), entry("reconnecting 5/5", 500), entry(ERROR, 650)])
    assert st["action"] == "SIT_2"
    assert st["target_prompt"]["text"] == prompt


def test_reconnect_with_no_visible_user_message_is_unknown_not_progress(app):
    st = state(app, [entry("正在重新连接5/5", 500), entry(ERROR, 650)])
    assert st["progress"] == "unknown"
    assert st["action"] == "SIT_1"
    assert st["target_prompt"] is None


def test_prompt_more_than_300_pixels_away_still_no_progress(app):
    st = state(app, [entry("继续", 250, 820, 880), entry("正在重新连接5/5", 650), entry(ERROR, 750)])
    assert st["action"] == "SIT_2"


def test_split_error_lines_merge_before_turn_classification(app):
    st = state(app, [PROMPT, entry("正在重新连接5/5", 530),
                     entry("rate limit exceeded: Your requests to gpt-6.1-sol", 620),
                     entry("exceeded token rate limit.", 650)])
    assert st["action"] == "SIT_2"
    assert "Your requests" in st["error"]["text"]
    assert st["error"]["top"] == 610


def test_real_content_after_error_prevents_old_retry(app):
    st = state(app, [PROMPT, entry(ERROR, 650), entry("已经恢复工作并补好了测试", 740, 820, 880)])
    assert st["action"] is None
    assert st["newer_prompt"]


@pytest.mark.parametrize("command", ["继续", "重试", "Retry"])
def test_card_command_after_error_does_not_block_retry_forever(app, command):
    """后续无新消息且仅有短指令时，应当稳定判定为情形二（SIT_2）。"""
    st = state(app, [PROMPT, entry(ERROR, 650), entry(command, 740, 820, 880)])
    assert not st["newer_prompt"]
    assert st["action"] == "SIT_2"

def test_repeated_card_button_still_has_same_signature(app):
    """卡片按钮不会改变签名，同一错误只按 2s 防抖处理，不会反复刷屏。"""
    first = state(app, [PROMPT, entry(ERROR, 650), entry("继续", 740, 820, 880)])
    second = state(app, [PROMPT, entry(ERROR, 650), entry("继续", 740, 820, 880)])
    assert first["sig"] == second["sig"]


def test_old_reconnect_does_not_override_new_active_thinking(app):
    st = state(app, [PROMPT, entry("正在重新连接5/5", 530), entry(ERROR, 650), entry("正在思考", 740)])
    assert st["busy"]
    assert not app.can_retry(st)


def test_low_confidence_work_is_not_proof_of_no_progress(app):
    st = state(app, [PROMPT, entry("已经修改了重试逻辑", 500, score=0.6), entry(ERROR, 650)])
    assert st["progress"] == "unknown"
    assert st["action"] == "SIT_1"


@pytest.mark.parametrize("draft", ["用户的草稿", "继续！", "rate limit exceeded", ["第一行用户草稿", "第二行用户草稿"]])
def test_user_draft_never_sent(app, draft):
    st = state(app, [PROMPT, entry(ERROR, 650)], draft=draft)
    assert st["user_draft"]
    assert not app.can_retry(st)


def test_manually_typed_continue_never_auto_submitted(app):
    st = state(app, [PROMPT, entry(ERROR, 650)], draft="继续")
    assert st["pending_input"]
    assert not app.can_retry(st)


def test_manual_edit_box_not_auto_submitted(app):
    st = state(app, [PROMPT, entry(ERROR, 650), entry("取消", 750, 700, 750), entry("发送", 750, 800, 850)])
    assert st["edit_open"]
    assert st["action"] is None
    assert not app.can_retry(st)


def test_no_geometric_composer_click_without_anchor(app):
    st = app.analyze_frame([line(ERROR, 650)], 1000, 1000, 1)
    assert st["input_pos"] is None
    assert st["send_pos"] is None


def test_loading_after_error_blocks_retry(app):
    st = state(app, [PROMPT, entry(ERROR, 650), entry("...", 740, 400, 440)])
    assert st["loading_more"]
    assert not app.can_retry(st)


def test_error_signature_ignores_y_and_dpi(app):
    first = state(app, [PROMPT, entry(ERROR, 650)])
    second = state(app, [entry("继续", 450, 820, 880), entry(ERROR, 700)], scale=1.5)
    assert first["sig"] == second["sig"]


def test_screenshot_fixture_detects_situation2_no_progress(app):
    """真实截图中只有重连和限流错误，无实质进展，应当判定为情形二（SIT_2）。"""
    fixture = json.loads((ROOT / "tests/fixtures/screenshot_no_progress.json").read_text(encoding="utf-8"))
    results = fixture["ocr_results"]
    st = app.analyze_frame(results, fixture["width"], fixture["height"], fixture["scale"])
    assert not st["newer_prompt"]
    assert st["action"] == "SIT_2"
    assert st["progress"] == "none"

def test_fixed_cooldown_has_no_growth_or_backoff_state():
    assert retry.CONFIG["RETRY_COOLDOWN"] == 2.0
    assert retry.MAX_ALLOWED_COOLDOWN <= 6.0
    assert "RATE_LIMIT_BACKOFF_FACTOR" not in retry.CONFIG
    assert "RETRY_RECOVERY_RESET" not in retry.CONFIG
    assert "RATE_LIMIT_PRE_WAIT" not in retry.CONFIG
    assert "RATE_LIMIT_MAX_WAIT" not in retry.CONFIG
    assert not hasattr(retry, "bounded_retry_delay")
    with pytest.raises(ValueError):
        retry.CONFIG["RETRY_COOLDOWN"] = 3.0


def test_retry_timings_are_fixed_and_within_allowed_limit():
    assert retry.CONFIG["POST_ACTION_COOLDOWN"] == 2.0
    assert retry.CONFIG["SAME_ERROR_GUARD"] == 2.0
    assert all(0 <= retry.CONFIG[key] <= 6.0 for key in
               ("RETRY_COOLDOWN", "POST_ACTION_COOLDOWN", "SAME_ERROR_GUARD"))


def test_config_validation_rejects_cooldown_above_six_seconds():
    original = retry.CONFIG.post_action_cooldown
    try:
        retry.CONFIG.post_action_cooldown = 7.0
        with pytest.raises(ValueError):
            retry.CONFIG.validate()
    finally:
        retry.CONFIG.post_action_cooldown = original
    retry.CONFIG.validate()


def test_retry_command_is_not_misclassified_as_status():
    assert not retry.CodexAutoRetryApp.is_status_line("重试")
    assert retry.CodexAutoRetryApp.is_status_line("正在重试 2/5")


def test_single_cli_entrypoint():
    source = (ROOT / "codex_auto_retry_v2.py").read_text(encoding="utf-8")
    assert source.count('if __name__ == "__main__":') == 1


@pytest.mark.parametrize("root", [0, 999])
def test_unknown_or_occluded_hit_test_never_clicks(app, monkeypatch, root):
    monkeypatch.setattr(retry, "get_foreground_root", lambda: 1)
    monkeypatch.setattr(retry, "get_window_rect", lambda hwnd: (0, 0, 1000, 1000))
    monkeypatch.setattr(retry, "root_window_at", lambda x, y: root)
    click = Mock()
    monkeypatch.setattr(retry, "click_screen_point", click)
    assert not app.safe_click(1, 500, 500, "test")
    click.assert_not_called()


def test_missing_pencil_template_has_no_default_geometric_fallback(app, monkeypatch):
    monkeypatch.setattr(retry.CONFIG, "_overrides", {"ALLOW_GEOMETRIC_PENCIL_FALLBACK": False})
    app.pencil_template = None
    point, how = app.find_pencil(np.zeros((1000, 1000, 3), dtype=np.uint8),
                                 {"right": 880, "bottom": 410}, 1)
    assert point is None


def test_unicode_input_structure_matches_windows_abi():
    assert retry.ctypes.sizeof(retry.INPUT) == (40 if retry.ctypes.sizeof(retry.ctypes.c_void_p) == 8 else 28)
    assert retry.u32.GetForegroundWindow.restype == retry.wintypes.HWND
    assert retry.g32.CreateCompatibleBitmap.restype == retry.wintypes.HBITMAP


def test_unicode_input_uses_correct_utf16_events_without_clipboard(monkeypatch):
    recorded = []
    def send(count, batch, size):
        recorded.extend((batch[i].ki.wScan, batch[i].ki.dwFlags) for i in range(count))
        assert size == retry.ctypes.sizeof(retry.INPUT)
        return count
    monkeypatch.setattr(retry.u32, "SendInput", send)
    assert retry.send_unicode_text("继续😀")
    assert len(recorded) == 8
    assert all(recorded[i][0] == recorded[i + 1][0] for i in range(0, 8, 2))
    assert recorded[0][1] == retry.KEYEVENTF_UNICODE
    assert recorded[1][1] == retry.KEYEVENTF_UNICODE | retry.KEYEVENTF_KEYUP


def test_send_input_partial_failure_is_not_success(monkeypatch):
    monkeypatch.setattr(retry.u32, "SendInput", lambda count, batch, size: count - 1)
    assert not retry.send_unicode_text("继续")


def test_negative_monitor_coordinates_are_kept(monkeypatch):
    metrics = {76: -1920, 77: -1080, 78: 4480, 79: 2520}
    monkeypatch.setattr(retry.u32, "GetSystemMetrics", metrics.__getitem__)
    assert retry.clip_to_virtual_desktop((-1600, -900, -600, -100)) == (-1600, -900, -600, -100)


def test_partially_offscreen_snapshot_is_not_used(monkeypatch):
    monkeypatch.setattr(retry.u32, "IsWindowVisible", lambda h: True)
    monkeypatch.setattr(retry.u32, "IsIconic", lambda h: False)
    monkeypatch.setattr(retry, "get_window_rect", lambda h: (-150, 0, 800, 800))
    monkeypatch.setattr(retry, "get_dpi_scale", lambda h: 1)
    monkeypatch.setattr(retry, "clip_to_virtual_desktop", lambda r: (0, 0, 800, 800))
    capture = Mock()
    monkeypatch.setattr(retry, "capture_window_gdi", capture)
    assert retry.capture_window_snapshot(1) is None
    capture.assert_not_called()


@pytest.mark.parametrize("argv,expected", [(["python.exe", "codex_auto_retry_v2.py"], "codex_auto_retry_v2.py"),
                                          (["python.exe", "-u", "codex_auto_retry_v2.py"], "codex_auto_retry_v2.py"),
                                          (["python.exe", "-X", "utf8", "a.py"], "a.py"),
                                          (["python.exe", "-c", "import codex_auto_retry_v2.py"], None),
                                          (["python.exe", "-m", "pytest", "codex_auto_retry_v2.py"], None)])
def test_process_matching_only_actual_script_argument(argv, expected):
    assert retry.python_script_argument(argv) == expected


# ---------------- 情形二执行路径：不依赖铅笔也能发出「继续」 ----------------

def fake_frame():
    return ((0, 0, 1000, 1000), np.zeros((1000, 1000, 3), np.uint8), [], 1.0)


def test_situation2_prefers_edit_pencil_resend(app, monkeypatch):
    """情形二（无进展）：优先点击铅笔图标展开编辑框，点击发送重新提交。"""
    st = state(app, [PROMPT, entry(ERROR, 650)])
    assert st["action"] == "SIT_2"
    app.current_state = Mock(return_value=(fake_frame(), st))
    monkeypatch.setattr(retry, "force_activate_window", lambda hwnd: True)
    assert retry.CONFIG["SIT2_PREFER_EDIT_RESEND"] is True

def test_situation2_pencil_missing_falls_back_to_composer(app, monkeypatch):
    st = state(app, [PROMPT, entry(ERROR, 650)])
    app.current_state = Mock(return_value=(fake_frame(), st))
    app.do_situation1 = Mock(return_value=True)
    app.find_pencil = Mock(return_value=(None, "铅笔模板未可靠匹配"))
    monkeypatch.setattr(retry, "force_activate_window", lambda hwnd: True)
    monkeypatch.setattr(retry.u32, "SetCursorPos", Mock())
    monkeypatch.setattr(retry, "root_window_at", lambda x, y: 123)
    monkeypatch.setitem(retry.CONFIG, "SIT2_PREFER_EDIT_RESEND", True)
    assert app.do_situation2(123, st["sig"]) is True
    app.do_situation1.assert_called_once_with(123, st["sig"], require_action=None)


def test_situation2_fallback_can_be_disabled(app, monkeypatch):
    st = state(app, [PROMPT, entry(ERROR, 650)])
    app.current_state = Mock(return_value=(fake_frame(), st))
    app.do_situation1 = Mock(return_value=True)
    app.find_pencil = Mock(return_value=(None, "铅笔模板未可靠匹配"))
    monkeypatch.setattr(retry, "force_activate_window", lambda hwnd: True)
    monkeypatch.setattr(retry.u32, "SetCursorPos", Mock())
    monkeypatch.setattr(retry, "root_window_at", lambda x, y: 123)
    monkeypatch.setitem(retry.CONFIG, "SIT2_PREFER_EDIT_RESEND", True)
    monkeypatch.setitem(retry.CONFIG, "SIT2_FALLBACK_TO_COMPOSER", False)
    assert app.do_situation2(123, st["sig"]) is False
    app.do_situation1.assert_not_called()


def test_find_pencil_locates_hover_icon(app):
    image = np.full((400, 600, 3), 24, np.uint8)
    template = retry.cv2.imread(str(ROOT / "pencil_template.png"))
    image[300:320, 500:520] = retry.cv2.cvtColor(template, retry.cv2.COLOR_BGR2RGB)
    prompt = {"right": 520, "bottom": 300, "left": 300, "top": 280, "cx": 410, "cy": 290}
    point, how = app.find_pencil(image, prompt, 1.0)
    assert point is not None, how
    assert abs(point[0] - 510) <= 6 and abs(point[1] - 310) <= 6, (point, how)


def test_find_pencil_returns_none_on_blank_frame(app, monkeypatch):
    monkeypatch.setattr(retry.CONFIG, "_overrides", {"ALLOW_GEOMETRIC_PENCIL_FALLBACK": False})
    blank = np.full((400, 600, 3), 24, np.uint8)
    prompt = {"right": 520, "bottom": 300, "left": 300, "top": 280, "cx": 410, "cy": 290}
    point, how = app.find_pencil(blank, prompt, 1.0)
    assert point is None


def test_scan_triggers_situation2_for_real_error_screenshot(app, monkeypatch):
    """真实截图中无进展时，scan_and_retry 应当触发情形二（SIT_2：点击铅笔重新编辑发送）。"""
    fixture = json.loads((ROOT / "tests/fixtures/screenshot_no_progress.json").read_text(encoding="utf-8"))
    results = fixture["ocr_results"]
    width, height, scale = fixture["width"], fixture["height"], fixture["scale"]
    frame = ((0, 0, width, height), np.zeros((height, width, 3), np.uint8), results, scale)
    window = {"hwnd": 11, "process": "chatgpt.exe", "title": "ChatGPT", "rect": (0, 0, width, height)}
    monkeypatch.setattr(retry, "find_target_windows", lambda: [window])
    monkeypatch.setattr(app, "select_target_window", lambda windows: windows[0])
    monkeypatch.setattr(app, "current_state",
                        lambda hwnd: (frame, app.analyze_frame(results, width, height, scale)))
    monkeypatch.setattr(app, "prepare_conversation_view", lambda hwnd, fr, st: (fr, st))
    monkeypatch.setattr(retry.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(retry.u32, "SetCursorPos", Mock())
    calls = []
    monkeypatch.setattr(app, "do_situation2",
                        lambda hwnd, sig=None:
                        calls.append((hwnd, "SIT_2")) or True)
    assert app.scan_and_retry() is True
    assert calls == [(11, "SIT_2")]
    assert app.mode2_count == 1

def test_situation1_types_continue_then_presses_enter(app, monkeypatch):
    """情形一主路径：点输入框 -> 写入继续 -> Enter -> 通过验证。"""
    base = state(app, [entry(ERROR, 650)])
    assert base["action"] == "SIT_1" and base["input_pos"]
    typed = dict(base, pending_input=True)
    after = dict(base, pending_input=False, busy=True)
    frame = fake_frame()
    app.current_state = Mock(side_effect=[(frame, base), (frame, base), (frame, typed), (frame, after)])
    app.safe_click = Mock(return_value=True)
    monkeypatch.setattr(retry, "force_activate_window", lambda hwnd: True)
    monkeypatch.setattr(retry, "send_unicode_text", Mock(return_value=True))
    monkeypatch.setattr(retry, "send_enter_key", Mock())
    monkeypatch.setattr(retry, "get_foreground_root", lambda: 123)
    monkeypatch.setattr(retry.time, "sleep", lambda seconds: None)
    assert app.do_situation1(123, base["sig"]) is True
    retry.send_unicode_text.assert_called_once_with(retry.CONFIG["AUTO_SEND_TEXT"])
    retry.send_enter_key.assert_called_once()


def test_situation1_aborts_when_text_not_in_composer(app, monkeypatch):
    base = state(app, [entry(ERROR, 650)])
    empty = dict(base, pending_input=False)
    frame = fake_frame()
    app.current_state = Mock(side_effect=[(frame, base), (frame, base), (frame, empty)])
    app.safe_click = Mock(return_value=True)
    monkeypatch.setattr(retry, "force_activate_window", lambda hwnd: True)
    monkeypatch.setattr(retry, "send_unicode_text", Mock(return_value=True))
    monkeypatch.setattr(retry, "send_enter_key", Mock())
    monkeypatch.setattr(retry, "get_foreground_root", lambda: 123)
    monkeypatch.setattr(retry.time, "sleep", lambda seconds: None)
    assert app.do_situation1(123, base["sig"]) is False
    retry.send_enter_key.assert_not_called()


def test_situation2_full_edit_flow_with_pencil_click_and_submit(app, monkeypatch):
    base = state(app, [PROMPT, entry(ERROR, 650)])
    assert base["action"] == "SIT_2"
    edited_open = dict(base, edit_open=True, edit_send=(500, 350))
    after_submit = dict(base, edit_open=False, busy=True)
    frame = fake_frame()
    app.current_state = Mock(side_effect=[
        (frame, base),
        (frame, edited_open),
        (frame, after_submit),
    ])
    app.find_pencil = Mock(return_value=((520, 310), "模板匹配 0.85"))
    app.safe_click = Mock(return_value=True)
    monkeypatch.setattr(retry, "force_activate_window", lambda hwnd: True)
    monkeypatch.setattr(retry.time, "sleep", lambda seconds: None)
    monkeypatch.setitem(retry.CONFIG, "SIT2_PREFER_EDIT_RESEND", True)
    assert app.do_situation2(123, base["sig"]) is True
    assert app.safe_click.call_count == 2


def test_edit_open_detected_with_only_cancel_button(app):
    prompt = entry("继续", 300, left=580, right=620)
    cancel = entry("取消", 340, left=530, right=570)
    st = state(app, [prompt, cancel, entry(ERROR, 650)])
    assert st["edit_open"] is True
    assert st["edit_send"] is None
