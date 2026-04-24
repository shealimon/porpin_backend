"""Greedy segment packing by approximate word count (for chunk-queue jobs)."""

from __future__ import annotations

import re


def count_words(text: str) -> int:
    return len(re.findall(r"\w+", text or "", flags=re.UNICODE))


def pack_segment_indices_by_words(
    segments: list[str],
    *,
    min_words_per_batch: int,
    max_words_per_batch: int,
) -> list[list[int]]:
    """
    Group segment indices into batches where each batch aims between ``min`` and ``max`` words.
    Single segments larger than ``max_words_per_batch`` still get their own batch.
    """
    if not segments:
        return []
    max_w = max(100, int(max_words_per_batch))
    min_w = max(0, int(min_words_per_batch))

    batches: list[list[int]] = []
    cur: list[int] = []
    cur_words = 0

    for i, seg in enumerate(segments):
        w = count_words(seg)
        if cur and cur_words + w > max_w:
            batches.append(cur)
            cur = []
            cur_words = 0
        cur.append(i)
        cur_words += w
    if cur:
        batches.append(cur)

    # Merge tiny trailing batches backward into previous when under min_w
    if min_w > 0 and len(batches) >= 2:
        i = len(batches) - 1
        while i > 0:
            w_prev = sum(count_words(segments[j]) for j in batches[i - 1])
            w_last = sum(count_words(segments[j]) for j in batches[i])
            if w_last < min_w and w_prev + w_last <= max_w:
                batches[i - 1].extend(batches[i])
                batches.pop(i)
            i -= 1
        i = 0
        while i < len(batches) - 1:
            w_first = sum(count_words(segments[j]) for j in batches[i])
            w_next = sum(count_words(segments[j]) for j in batches[i + 1])
            if w_first < min_w and w_first + w_next <= max_w:
                batches[i].extend(batches[i + 1])
                batches.pop(i + 1)
                continue
            i += 1

    return batches
