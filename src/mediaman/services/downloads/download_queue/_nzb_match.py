"""NZBGet-to-arr matching helpers."""

from __future__ import annotations


def nzb_matches_arr(nzb_t_norm: str, arr_candidates: list[str]) -> bool:
    """Return True if *nzb_t_norm* matches any candidate in *arr_candidates*.

    Performs a bidirectional substring test so both "married at first sight
    au" ⊂ longer NZB titles and the reverse work correctly.  *arr_candidates*
    is a list of normalised strings built from the arr item's primary title
    and any release names Sonarr/Radarr recorded.
    """
    for cand in arr_candidates:
        if cand in nzb_t_norm or nzb_t_norm in cand:
            return True
    return False
