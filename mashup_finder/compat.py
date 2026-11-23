"""Tempo and key compatibility maths. Pure functions, no I/O.

A pair is directional: the VOCAL comes from track A and is warped (time-stretched and
pitch-shifted) onto the INSTRUMENTAL of track B, which stays untouched.
"""
import re
import numpy as np

PITCH = {"C": 0, "C#": 1, "Db": 1, "D": 2, "D#": 3, "Eb": 3, "E": 4, "F": 5, "F#": 6, "Gb": 6,
         "G": 7, "G#": 8, "Ab": 8, "A": 9, "A#": 10, "Bb": 10, "B": 11}
NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# Penalty for each harmonic relation between the shifted vocal key and the instrumental key.
REL_PENALTY = {"same": 0.0, "relative": 0.15, "fifth": 0.35}
# Cost per semitone of pitch shift on the vocal (formant-preserving shift still degrades it).
SHIFT_COST = 0.3
MAX_SHIFT = 2

TEMPO_MULTS = (1.0, 2.0, 0.5)


def parse_key(s):
    """'G minor' / 'F# major' / '6A' -> index 0..23 (pc + 12*minor), or None."""
    if not s:
        return None
    s = s.strip().strip("'\"")
    m = re.fullmatch(r"([A-G][#b]?)(m|min|maj)?", s)          # rekordbox / tag style: Gm, F#, Abmaj
    if m and m.group(1) in PITCH:
        return PITCH[m.group(1)] + (12 if m.group(2) in ("m", "min") else 0)
    parts = s.split()
    if len(parts) == 2 and parts[0] in PITCH and parts[1].lower() in ("major", "minor"):
        return PITCH[parts[0]] + (12 if parts[1].lower() == "minor" else 0)
    if len(s) in (2, 3) and s[-1] in "AB" and s[:-1].isdigit():
        n = int(s[:-1])
        if 1 <= n <= 12:
            # Camelot: 8B = C major, 5A = C minor; +1 step = +7 semitones
            if s[-1] == "B":
                return (7 * (n - 8)) % 12
            return (7 * (n - 5)) % 12 + 12
    return None


def key_name(k):
    return f"{NAMES[k % 12]} {'minor' if k >= 12 else 'major'}"


def camelot(k):
    # inverse of parse_key (7 is its own inverse mod 12): C major = 8B, C minor = 5A
    pc, minor = k % 12, k >= 12
    n = ((5 if minor else 8) + 7 * pc - 1) % 12 + 1
    return f"{n}{'A' if minor else 'B'}"


def relation(a, b):
    """Harmonic relation of key a to key b (both 0..23), or None if they clash."""
    if a == b:
        return "same"
    pa, pb, ma, mb = a % 12, b % 12, a >= 12, b >= 12
    if ma != mb:
        minor_pc, major_pc = (pa, pb) if ma else (pb, pa)
        if (minor_pc + 3) % 12 == major_pc:
            return "relative"
        return None
    if (pa - pb) % 12 in (5, 7):
        return "fifth"
    return None


def _build_key_table():
    """KEY_COST[a, b] = cheapest cost to make vocal key a fit instrumental key b; KEY_SHIFT the semitones."""
    cost = np.full((24, 24), np.inf)
    shift = np.zeros((24, 24), dtype=np.int8)
    rel = np.empty((24, 24), dtype=object)
    for a in range(24):
        for b in range(24):
            for s in range(-MAX_SHIFT, MAX_SHIFT + 1):
                a2 = (a % 12 + s) % 12 + (12 if a >= 12 else 0)
                r = relation(a2, b)
                if r is None:
                    continue
                c = REL_PENALTY[r] + SHIFT_COST * abs(s)
                if c < cost[a, b] - 1e-9 or (abs(c - cost[a, b]) < 1e-9 and abs(s) < abs(shift[a, b])):
                    cost[a, b], shift[a, b], rel[a, b] = c, s, r
    return cost, shift, rel


KEY_COST, KEY_SHIFT, KEY_REL = _build_key_table()


def tempo_match(bpm_vocal, bpm_inst):
    """Best (stretch_fraction, multiplier) to bring the vocal onto the instrumental tempo.
    stretch = |inst / (vocal * mult) - 1|; mult 2 means the vocal runs double-time over the beat."""
    best = (np.inf, 1.0)
    for m in TEMPO_MULTS:
        s = abs(bpm_inst / (bpm_vocal * m) - 1.0)
        if s < best[0]:
            best = (s, m)
    return best
