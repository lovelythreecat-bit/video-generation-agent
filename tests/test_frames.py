import pytest

from video_agent.errors import VideoAgentError
from video_agent.frames import redistribute


def test_redistribute_scales_equal_shots():
    assert redistribute([150, 150], 150) == [75, 75]


def test_redistribute_adds_remainder_to_lowest_index():
    assert redistribute([1, 1, 1], 4) == [2, 1, 1]


def test_redistribute_removes_excess_from_lowest_index():
    assert redistribute([2, 2], 3) == [1, 2]


def test_redistribute_scales_one_shot():
    assert redistribute([300], 450) == [450]


def test_redistribute_rejects_more_shots_than_frames():
    with pytest.raises(VideoAgentError) as caught:
        redistribute([1, 1], 1)
    assert caught.value.code == "VALIDATION_ERROR"
