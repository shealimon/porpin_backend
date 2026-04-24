"""Greedy segment packing by token count (tiktoken) for chunk-queue jobs."""

from __future__ import annotations

from app.utils.chunking import count_tokens


def pack_segment_indices_by_tokens(
    segments: list[str],
    *,
    min_tokens_per_batch: int,
    max_tokens_per_batch: int,
    overhead_per_seg: int = 80,
) -> list[list[int]]:
    """
    Group segment indices into batches bounded by estimated input tokens per batch.

    ``overhead_per_seg`` approximates delimiter / JSON framing cost in multi-segment prompts.
    """
    if not segments:
        return []
    max_t = max(200, int(max_tokens_per_batch))
    min_t = max(0, int(min_tokens_per_batch))

    batches: list[list[int]] = []
    cur: list[int] = []
    cur_tok = 0

    for i, seg in enumerate(segments):
        tok = count_tokens(seg) + overhead_per_seg
        if cur and cur_tok + tok > max_t:
            batches.append(cur)
            cur = []
            cur_tok = 0
        cur.append(i)
        cur_tok += tok
    if cur:
        batches.append(cur)

    def batch_tokens(idxs: list[int]) -> int:
        return sum(count_tokens(segments[j]) + overhead_per_seg for j in idxs)

    if min_t > 0 and len(batches) >= 2:
        i = len(batches) - 1
        while i > 0:
            t_prev = batch_tokens(batches[i - 1])
            t_last = batch_tokens(batches[i])
            if t_last < min_t and t_prev + t_last <= max_t:
                batches[i - 1].extend(batches[i])
                batches.pop(i)
            i -= 1
        i = 0
        while i < len(batches) - 1:
            t_first = batch_tokens(batches[i])
            t_next = batch_tokens(batches[i + 1])
            if t_first < min_t and t_first + t_next <= max_t:
                batches[i].extend(batches[i + 1])
                batches.pop(i + 1)
                continue
            i += 1

    return batches
