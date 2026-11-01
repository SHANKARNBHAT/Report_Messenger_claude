"""
Checks of volume_profile.py on small made-up data, with the answers worked out by hand.

Run: python test_volume_profile.py
"""

import numpy as np
import pandas as pd

import volume_profile as vp


def bars(rows, start="2026-10-26 09:15"):
    index = pd.date_range(start, periods=len(rows), freq="15min", tz=vp.IST)
    return pd.DataFrame(rows, columns=["high", "low", "close", "volume"], index=index, dtype=float)


def test_spread_evenly_over_the_range():
    # 1000 over 100 to 104 at a bin of 1: 250 in each of the four bins.
    edges, bins = vp.profile(np.array([104.0]), np.array([100.0]), np.array([1000.0]), 1.0)
    assert edges.tolist() == [100, 101, 102, 103, 104]
    assert np.allclose(bins, [250, 250, 250, 250])


def test_bar_covering_part_of_a_bin():
    # 90 over 100.5 to 103.5: a sixth, a third, a third and a sixth of a 3-point range.
    edges, bins = vp.profile(np.array([103.5]), np.array([100.5]), np.array([90.0]), 1.0)
    assert edges.tolist() == [100, 101, 102, 103, 104]
    assert np.allclose(bins, [15, 30, 30, 15])


def test_bar_with_one_price():
    edges, bins = vp.profile(np.array([100.0, 102.0, 102.0]), np.array([100.0, 100.0, 102.0]),
                             np.array([7.0, 10.0, 3.0]), 1.0)
    assert edges.tolist() == [100, 101, 102]
    assert np.allclose(bins, [12, 8])  # the bar at 102, the very top, stays in the top bin
    assert np.isclose(bins.sum(), 20)  # no volume lost


def test_value_area_by_hand():
    #        index:   0   1   2   3   4   5   6      total 200, 70% = 140
    bins = np.array([10, 20, 30, 60, 40, 30, 10], dtype=float)
    # POC 3 (60) -> above 40 (100) -> below 30 = above 30, so both (160): done.
    assert vp.value_area(bins) == (3, 2, 5)


def test_value_area_stops_at_the_edge():
    bins = np.array([100, 5, 5, 5], dtype=float)  # the POC alone holds 87% of the volume
    assert vp.value_area(bins) == (0, 0, 0)
    bins = np.array([60, 30, 5, 5], dtype=float)  # the POC is the bottom bin: only up from there
    assert vp.value_area(bins) == (0, 0, 1)


def test_value_area_crosses_an_empty_gap_on_both_sides():
    bins = np.array([30, 0, 40, 0, 30], dtype=float)  # 100 in all, 70% = 70
    assert vp.value_area(bins) == (2, 0, 4)  # a tie at zero widens both ways, not just up


def test_tied_bins_take_the_middle():
    assert vp.value_area(np.array([10, 10, 10, 10, 10], dtype=float))[0] == 2


def test_levels_by_hand():
    # A busy block of 700 on 1000 to 1010 and 300 on 1010 to 1020; bin 2.5 from a close of 1000.
    table = bars([(1010, 1000, 1000, 700), (1020, 1010, 1000, 300)])
    found = vp.levels(table)
    # Bins of 175 from 1000 to 1010, then 75. 70% of 1000 = 700: exactly the first four bins.
    assert 1000 < found["POC"] < 1010
    assert (found["VAL"], found["VAH"]) == (1000, 1010)


def test_zero_volume_bars_do_not_stretch_the_range():
    table = bars([(1010, 1000, 1000, 100), (2000, 1, 1000, 0)])
    assert vp.levels(table)["VAH"] <= 1010


def hours(volumes, start="2026-08-03"):
    """60-minute bars: a list per day of its (9:15, 10:15) volumes."""
    days = pd.bdate_range(start, periods=len(volumes))
    index = [d + pd.Timedelta(hours=h, minutes=15) for d in days for h in (9, 10)]
    table = pd.DataFrame({"volume": np.array(volumes, dtype=float).ravel()}, index=pd.DatetimeIndex(index).tz_localize(vp.IST))
    return table, days


def one_a_day(days, volumes):
    index = pd.DatetimeIndex(days + pd.Timedelta(hours=9, minutes=15)).tz_localize(vp.IST)
    return pd.DataFrame({"volume": np.array(volumes, dtype=float)}, index=index)


def test_fill_open_volume():
    # Six days of 800 outside the 9:15 bar. The intraday bars hold 90% of a day of 1000.
    hourly, days = hours([[0, 800], [0, 800], [50, 800], [0, 900], [0, 800], [0, 800]])
    daily = one_a_day(days, [1000, 1000, 1000, 1200, 1000, 1000])
    fine = one_a_day(days, [900] * 6)
    filled = vp.fill_open_volume(hourly, daily, fine)["volume"].to_numpy()
    assert filled[::2].tolist() == [100, 100, 50, 180, 100, 100]  # 90% of the day less the rest; 50 was known
    assert filled[1::2].tolist() == [800, 800, 800, 900, 800, 800]  # other bars left as they were


def test_fill_open_volume_with_a_bad_daily_volume():
    hourly, days = hours([[0, 800]] * 11)
    daily = one_a_day(days, [1000] * 5 + [10] + [1000] * 5)
    filled = vp.fill_open_volume(hourly, daily, one_a_day(days, [900] * 11))["volume"]
    assert filled.to_numpy()[10] == 100  # the stock's usual 1/8 of the other hours, not zero


def test_fill_open_volume_before_a_split():
    # A 1:5 split after day 12: the daily bars count old shares five times, the 60-minute ones don't.
    hourly, days = hours([[0, 160]] * 12 + [[0, 800]] * 24)
    daily = one_a_day(days, [1000] * 36)
    fine = one_a_day(days[12:], [900] * 24)
    filled = vp.fill_open_volume(hourly, daily, fine)["volume"].to_numpy()
    assert np.allclose(filled[1:14:2], 800)  # well before the split: five times the 160
    assert np.allclose(filled[0:14:2], 100)
    assert np.allclose(filled[60::2], 100) and np.allclose(filled[61::2], 800)  # after it: as it was


def test_after_last_gap():
    index = pd.date_range("2026-04-29 14:15", periods=4, freq="h", tz=vp.IST)
    table = pd.DataFrame({"high": [778, 779, 289, 285], "low": [766, 771, 271, 275],
                          "close": [772, 775, 285, 277]}, index=index, dtype=float)
    assert vp.after_last_gap(table).index[0] == index[2]  # like VEDL's demerger
    table = table.assign(high=[778, 779, 700, 690], low=[766, 771, 650, 660], close=[772, 775, 660, 680])
    assert len(vp.after_last_gap(table)) == 4  # a fall of 10% is the market


def test_periods():
    def spans(day):
        return {k: (f"{a:%Y-%m-%d}", f"{b:%Y-%m-%d}") for k, (a, b) in vp.period_spans(pd.Timestamp(day)).items()}

    midweek = spans("2026-10-28")  # a Wednesday
    assert midweek["Weekly"] == ("2026-10-19", "2026-10-23")
    assert midweek["Monthly"] == ("2026-09-01", "2026-09-30")
    assert midweek["3M"] == ("2026-07-29", "2026-10-28")
    assert midweek["1Y"] == ("2025-10-29", "2026-10-28")
    friday = spans("2026-10-30")  # the week is over, and the month too: the 31st is a Saturday
    assert friday["Weekly"] == ("2026-10-26", "2026-10-30")
    assert friday["Monthly"] == ("2026-10-01", "2026-10-31")
    thursday = spans("2026-10-29")
    assert thursday["Weekly"] == ("2026-10-19", "2026-10-23")
    assert thursday["Monthly"] == ("2026-09-01", "2026-09-30")
    assert spans("2026-11-01")["Weekly"] == ("2026-10-26", "2026-10-30")  # a Sunday
    assert spans("2026-12-31")["Monthly"] == ("2026-12-01", "2026-12-31")  # year end
    assert spans("2026-12-31")["6M"] == ("2026-07-01", "2026-12-31")


def test_settled_day():
    assert vp.last_settled_day(pd.Timestamp("2026-10-28 15:00", tz=vp.IST)) == pd.Timestamp("2026-10-27")
    assert vp.last_settled_day(pd.Timestamp("2026-10-28 16:45", tz=vp.IST)) == pd.Timestamp("2026-10-28")


def test_new_listing_left_blank_for_longer_periods():
    days = pd.bdate_range("2026-10-05", "2026-10-30")
    index = pd.DatetimeIndex([d + pd.Timedelta(hours=9, minutes=15) for d in days]).tz_localize(vp.IST)
    table = pd.DataFrame({"high": 110.0, "low": 100.0, "close": 105.0, "volume": 1000.0}, index=index)
    row = vp.stock_levels("NEW", table, pd.Timestamp("2026-10-30"))
    assert row["Weekly POC"] is not None and row["Monthly POC"] is not None
    assert row["3M POC"] is None and row["1Y VAL"] is None


if __name__ == "__main__":
    tests = [(name, check) for name, check in globals().items() if name.startswith("test_")]
    for name, check in tests:
        check()
        print("ok ", name)
    print(f"{len(tests)} checks passed")
