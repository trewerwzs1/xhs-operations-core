"""Auditable pacing bounds for visible actions, reading and scrolling."""

import random
import os
import time

# ========== 配置常量 ==========
DEFAULT_MAX_ATTEMPTS = 24
STAGNANT_LIMIT = 6
MIN_SCROLL_DELTA = 10
MAX_CLICK_PER_ROUND = 1
STAGNANT_CHECK_THRESHOLD = 2
LARGE_SCROLL_TRIGGER = 3
BUTTON_CLICK_INTERVAL = 3
FINAL_SPRINT_PUSH_COUNT = 1

# ========== 延迟范围（毫秒） ==========
HUMAN_DELAY = (300, 700)
REACTION_TIME = (300, 800)
HOVER_TIME = (100, 300)
READ_TIME = (500, 1200)
SHORT_READ = (600, 1200)
SCROLL_WAIT = (100, 200)
POST_SCROLL = (300, 500)

VISIBLE_ACTION_DELAY_MS = (10_000, 15_000)
READING_DURATION_MS = (10_000, 15_000)
TEST_DELAY_ENV = "TONYREDBOOK_TEST_DELAY_MS"
TEST_MODE_ENV = "TONYREDBOOK_TEST_MODE"


def sleep_random(min_ms: int, max_ms: int) -> None:
    """随机延迟。"""
    if max_ms <= min_ms:
        time.sleep(min_ms / 1000.0)
        return
    delay = random.randint(min_ms, max_ms) / 1000.0
    time.sleep(delay)


def navigation_delay() -> None:
    """Apply the configured observation interval after navigation."""
    visible_action_delay()


def visible_action_delay() -> None:
    """Wait after a user-visible action; live mode is always 10–15 seconds."""
    test_delay = os.environ.get(TEST_DELAY_ENV)
    if test_delay is not None:
        if os.environ.get(TEST_MODE_ENV) != "1":
            raise RuntimeError(
                f"{TEST_DELAY_ENV} is test-only and requires {TEST_MODE_ENV}=1"
            )
        delay_ms = max(0, int(test_delay))
        time.sleep(delay_ms / 1000.0)
        return
    sleep_random(*VISIBLE_ACTION_DELAY_MS)


def get_scroll_interval(speed: str) -> float:
    """Return a deterministic, reviewable interval between bounded scrolls."""
    if speed == "slow":
        return 1.2
    if speed == "fast":
        return 0.3
    # normal
    return 0.6


def get_scroll_ratio(speed: str) -> float:
    """根据速度获取滚动比例。"""
    if speed == "slow":
        return 0.5
    if speed == "fast":
        return 0.9
    return 0.7


def calculate_scroll_delta(viewport_height: int, base_ratio: float) -> float:
    """Return one deterministic viewport-relative scroll, clamped to safe bounds."""
    if type(viewport_height) is not int or viewport_height <= 0:
        raise ValueError("viewport_height must be a positive integer")
    if not isinstance(base_ratio, (int, float)):
        raise ValueError("base_ratio must be numeric")
    ratio = max(0.35, min(0.85, float(base_ratio)))
    return float(max(320, min(int(viewport_height * 0.85), int(viewport_height * ratio))))


# 页面不可访问关键词
INACCESSIBLE_KEYWORDS = [
    "当前笔记暂时无法浏览",
    "该内容因违规已被删除",
    "该笔记已被删除",
    "内容不存在",
    "笔记不存在",
    "已失效",
    "私密笔记",
    "仅作者可见",
    "因用户设置，你无法查看",
    "因违规无法查看",
]
