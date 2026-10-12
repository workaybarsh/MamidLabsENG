"""Chronological merge (NO timestamps in the text). Output shape:

    (footsteps)
    Speaker: Did you come?
    (door closing)
    Speaker: Yes, a bit late.

Speech lines use "Speaker" (or "Speaker N" when diarized); events appear in parentheses.
Lines are ordered CHRONOLOGICALLY only; timestamps are not written to the text
(enable with `include_timestamps=True`).
"""
from __future__ import annotations


def timestamp(seconds: float) -> str:
    total = int(round(seconds))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return '%02d:%02d:%02d' % (hours, minutes, secs)
    return '%02d:%02d' % (minutes, secs)


def _speaker_for(start: float, end: float, turns: list[dict]) -> str | None:
    """Returns the speaker with the largest overlap with the speech segment."""
    best = None
    best_overlap = 0.0
    for turn in turns or []:
        overlap = min(end, turn['end']) - max(start, turn['start'])
        if overlap > best_overlap:
            best_overlap = overlap
            best = turn['speaker']
    return best


def merge(segments: list[dict], events: list[dict], turns: list[dict] | None = None,
          speaker_prefix: str = 'Speaker', include_timestamps: bool = False) -> str:
    rows: list[tuple[float, int, str, dict]] = []
    for segment in segments or []:
        rows.append((float(segment['start']), 0, 'speech', segment))
    for event in events or []:
        rows.append((float(event['start']), 1, 'event', event))
    # Chronological: when a speech line and an event share the same start, speech first.
    rows.sort(key=lambda row: (row[0], row[1]))

    lines: list[str] = []
    for start, _, kind, obj in rows:
        prefix = '[%s] ' % timestamp(start) if include_timestamps else ''
        if kind == 'speech':
            label = speaker_prefix
            speaker = _speaker_for(obj['start'], obj['end'], turns or [])
            if speaker:
                # 'SPEAKER_00' -> '1'
                num = ''.join(ch for ch in speaker if ch.isdigit())
                label = '%s %d' % (speaker_prefix, int(num) + 1) if num else speaker_prefix
            lines.append('%s%s: %s' % (prefix, label, obj['text']))
        else:
            lines.append('%s(%s)' % (prefix, obj['text']))
    return '\n'.join(lines)
