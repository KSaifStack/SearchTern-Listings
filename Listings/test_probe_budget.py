"""Tests for per-run probe budgeting (oldest-first rotation + cap)."""
from probe_budget import _timestamp, oldest_first

BOARDS = [("Co", "Greenhouse", f"slug{i}") for i in range(10)]


def test_never_probed_boards_go_first():
    last = {f"Greenhouse|slug{i}": 100.0 for i in range(5, 10)}
    batch, deferred = oldest_first(BOARDS, last, cap=5)
    assert [b[2] for b in batch] == [f"slug{i}" for i in range(5)]
    assert len(deferred) == 5


def test_oldest_first_not_list_order():
    last = {f"Greenhouse|slug{i}": float(100 - i) for i in range(10)}
    batch, _ = oldest_first(BOARDS, last, cap=3)
    assert [b[2] for b in batch] == ["slug9", "slug8", "slug7"]


def test_no_cap_keeps_everything():
    batch, deferred = oldest_first(BOARDS, {}, cap=None)
    assert len(batch) == 10 and deferred == []


def test_zero_and_negative_cap_mean_unlimited():
    assert len(oldest_first(BOARDS, {}, cap=0)[0]) == 10
    assert len(oldest_first(BOARDS, {}, cap=-3)[0]) == 10


def test_cap_larger_than_registry_is_a_noop():
    batch, deferred = oldest_first(BOARDS, {}, cap=50)
    assert len(batch) == 10 and deferred == []


def test_dict_entries_use_their_timestamp():
    assert _timestamp({"at": 12.5, "slugs": []}) == 12.5
    assert _timestamp({"slugs": []}) == 0.0
    assert _timestamp(None) == 0.0
    assert _timestamp(True) == 0.0


def test_deferred_boards_stay_old_so_the_next_run_picks_them_up():
    boards = [("Co", "Greenhouse", f"slug{i}") for i in range(4)]
    last = {"Greenhouse|slug0": 4.0, "Greenhouse|slug1": 3.0,
            "Greenhouse|slug2": 2.0, "Greenhouse|slug3": 1.0}
    first, deferred = oldest_first(boards, last, cap=2)
    assert [b[2] for b in first] == ["slug3", "slug2"]
    for board in first:
        last[f"{board[1]}|{board[2]}"] = 99.0
    # deferred keep slug3=1.0? no: slug2/slug3 were consumed; remaining are 0/1
    second, _ = oldest_first(deferred, last, cap=2)
    assert [b[2] for b in second] == ["slug1", "slug0"]


def test_rotation_probes_every_board_without_starvation():
    boards = [("Co", "Greenhouse", f"slug{i}") for i in range(12)]
    last: dict[str, float] = {}
    seen: list[str] = []
    for run in range(4):
        batch, _ = oldest_first(boards, last, cap=3)
        for board in batch:
            # a probed board must sort *after* the boards still waiting, which is
            # exactly what a real wall-clock stamp does
            last[f"{board[1]}|{board[2]}"] = float(run + 1)
            seen.append(board[2])
        assert len(batch) == 3
    assert sorted(seen) == sorted({b[2] for b in boards})
