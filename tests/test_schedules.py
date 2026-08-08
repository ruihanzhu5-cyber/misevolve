import pytest

from misevolve.bench.schedules import MIXING_SCHEDULES, TIMING_SCHEDULES, schedule_blocks


@pytest.mark.parametrize("name", TIMING_SCHEDULES + MIXING_SCHEDULES)
def test_schedule_preserves_task_counts(name):
    blocks = schedule_blocks(name, 7)
    assert len(blocks) == 6
    assert sum(block.count("M") for block in blocks) == 9
    assert sum(block.count("B") for block in blocks) == 9
