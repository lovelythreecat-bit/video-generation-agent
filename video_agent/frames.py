from video_agent.errors import VideoAgentError


def _round_half_away(numerator: int, denominator: int) -> int:
    return (2 * numerator + denominator) // (2 * denominator)


def _longest_index(values: list[int], minimum: int) -> int | None:
    best_at: int | None = None
    best_value: int | None = None
    for index, value in enumerate(values):
        if value < minimum:
            continue
        if best_value is None or value > best_value:
            best_value = value
            best_at = index
    return best_at


def redistribute(lengths: list[int], new_total: int) -> list[int]:
    if new_total < 1 or any(length < 1 for length in lengths):
        raise VideoAgentError("VALIDATION_ERROR", "帧数必须是正整数")
    if len(lengths) > new_total:
        raise VideoAgentError("VALIDATION_ERROR", "镜头数大于总帧数")
    if len(lengths) == 0:
        raise VideoAgentError("VALIDATION_ERROR", "没有镜头可分摊")
    old_total = sum(lengths)
    assigned = [max(1, _round_half_away(length * new_total, old_total)) for length in lengths]
    guard = new_total + len(lengths)
    while sum(assigned) > new_total:
        guard -= 1
        index = _longest_index(assigned, 2)
        if guard < 0 or index is None:
            raise VideoAgentError("VALIDATION_ERROR", "无法在每镜至少 1 帧的前提下扣平")
        assigned[index] -= 1
    while sum(assigned) < new_total:
        index = _longest_index(assigned, 1)
        if index is None:
            raise VideoAgentError("VALIDATION_ERROR", "无法在每镜至少 1 帧的前提下扣平")
        assigned[index] += 1
    return assigned
