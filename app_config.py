#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""集中配置：固定 2 秒重试，无指数退避、无累计状态。"""

from dataclasses import dataclass, field
import math
from typing import Any, Dict, Iterator, Tuple

MAX_ALLOWED_COOLDOWN = 6.0
FIXED_RETRY_COOLDOWN = 2.0


@dataclass
class AppConfig:
    poll_interval: float = 2.0
    retry_cooldown: float = field(default=FIXED_RETRY_COOLDOWN, init=False)
    post_action_cooldown: float = 2.0
    same_error_guard: float = 2.0
    restore_mouse_pos: bool = True
    auto_send_text: str = "继续"

    auto_scroll_enabled: bool = True
    target_active_window_only: bool = True
    scroll_check_interval: float = 2.0
    scroll_bottom_tolerance_dip: float = 16.0
    scroll_wheel_notches: int = 5
    max_scroll_steps_per_cycle: int = 4
    scroll_min_wait: float = 0.45
    scroll_stable_timeout: float = 2.5
    scroll_stable_interval: float = 0.25
    scroll_stable_frames: int = 2
    scroll_stable_threshold: float = 1.2
    scroll_stall_cooldown: float = 30.0
    send_verify_wait: float = 0.6

    error_keywords: Tuple[str, ...] = (
        "ratelimitexceeded", "exceededtokenratelimit", "badresponsestatuscode",
        "failedtofetch", "statuscode429", "statuscode500", "statuscode502",
        "statuscode503", "statuscode504", "toomanyrequests",
    )
    busy_keywords: Tuple[str, ...] = (
        "正在思考", "思考中", "深度思考", "thinking",
        "正在生成", "正在编写", "正在处理", "正在搜索", "正在分析",
        "正在读取", "正在核验", "generating", "searching",
    )
    user_prompt_keywords: Tuple[str, ...] = ("继续", "重试", "Retry", "Continue")
    reconnect_keywords: Tuple[str, ...] = ("正在重新连接", "正在重连", "reconnecting")

    target_processes: Tuple[str, ...] = ("chatgpt.exe", "codex.exe")
    exclude_processes: Tuple[str, ...] = (
        "antigravity.exe", "code.exe", "cursor.exe",
        "powershell.exe", "cmd.exe", "windowsterminal.exe", "devenv.exe",
    )
    min_ocr_score: float = 0.80

    input_above_toolbar_dip: float = 51.0
    send_right_of_model_dip: float = 40.0
    pencil_below_bubble_dip: float = 20.0
    allow_geometric_pencil_fallback: bool = True
    sit2_fallback_to_composer: bool = True   # 铅笔不可用时改为输入框发送「继续」
    sit2_prefer_edit_resend: bool = True    # 默认不依赖铅笔，直接发送「继续」
    pencil_match_threshold: float = 0.65
    pencil_match_threshold_masked: float = 0.58

    _overrides: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @staticmethod
    def _field_name(key: str) -> str:
        return key.lower()

    def __getitem__(self, key: str) -> Any:
        if key in self._overrides:
            return self._overrides[key]
        if key == "RETRY_COOLDOWN":
            return FIXED_RETRY_COOLDOWN
        return getattr(self, self._field_name(key))

    def __setitem__(self, key: str, value: Any) -> None:
        if key == "RETRY_COOLDOWN":
            raise ValueError("重试冷却固定为 2 秒，不可覆盖")
        self._overrides[key] = value

    def __delitem__(self, key: str) -> None:
        self._overrides.pop(key, None)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and (
            key in self._overrides or hasattr(self, key.lower())
        )

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except (AttributeError, KeyError):
            return default

    def keys(self) -> Iterator[str]:
        return iter(name.upper() for name in self.__dataclass_fields__ if name != "_overrides")

    def items(self):
        return ((key, self[key]) for key in self.keys())

    def validate(self) -> None:
        numeric_positive = (
            "poll_interval", "post_action_cooldown", "same_error_guard",
            "scroll_check_interval", "scroll_stable_timeout",
            "scroll_stable_interval", "send_verify_wait",
        )
        for name in numeric_positive:
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"配置项 {name.upper()} 必须为正数")
        for name in ("post_action_cooldown", "same_error_guard"):
            if float(getattr(self, name)) > MAX_ALLOWED_COOLDOWN:
                raise ValueError(f"{name.upper()} 不得超过 {MAX_ALLOWED_COOLDOWN:g}s")
        if self.retry_cooldown != FIXED_RETRY_COOLDOWN:
            raise ValueError("重试冷却固定为 2 秒")
        if not 0 <= float(self.min_ocr_score) <= 1:
            raise ValueError("MIN_OCR_SCORE 必须位于 0..1")
        for name in ("scroll_wheel_notches", "max_scroll_steps_per_cycle", "scroll_stable_frames"):
            if int(getattr(self, name)) < 1:
                raise ValueError(f"配置项 {name.upper()} 必须大于 0")
        if not self.target_processes:
            raise ValueError("TARGET_PROCESSES 不得为空")


CONFIG = AppConfig()
CONFIG.validate()
