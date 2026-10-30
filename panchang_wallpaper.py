"""
Panchang wallpaper: the day's Panchang for Bengaluru, drawn as a phone lock-screen
image (1080 x 2400, the top third left clear for the clock).

.github/workflows/panchang.yml runs it every night just after midnight and publishes
the image on the "panchang" branch; the phone downloads it each morning and sets it
as the lock screen (MacroDroid).

The positions of the Sun and Moon come from NASA's DE421 ephemeris (Skyfield),
sidereal with the Lahiri ayanamsa. Each element is the one at sunrise, with the time
it changes before the next sunrise. The lunar month is amanta (new moon to new moon).

    python panchang_wallpaper.py OUT.png [--date YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont
from skyfield import almanac
from skyfield.api import Loader, wgs84
from skyfield.framelib import ecliptic_frame

PLACE = ("Bengaluru", 12.9716, 77.5946)
IST = ZoneInfo("Asia/Kolkata")
SIZE = (1080, 2400)
SCALE = 2  # drawn at twice the size, then reduced: smooth edges
TOP = 860  # the lock screen's clock and date sit above this

TITHIS = ["Pratipada", "Dwitiya", "Tritiya", "Chaturthi", "Panchami", "Shashthi", "Saptami", "Ashtami",
          "Navami", "Dashami", "Ekadashi", "Dwadashi", "Trayodashi", "Chaturdashi"]
NAKSHATRAS = ["Ashwini", "Bharani", "Krittika", "Rohini", "Mrigashira", "Ardra", "Punarvasu", "Pushya",
              "Ashlesha", "Magha", "Purva Phalguni", "Uttara Phalguni", "Hasta", "Chitra", "Swati",
              "Vishakha", "Anuradha", "Jyeshtha", "Mula", "Purva Ashadha", "Uttara Ashadha", "Shravana",
              "Dhanishta", "Shatabhisha", "Purva Bhadrapada", "Uttara Bhadrapada", "Revati"]
YOGAS = ["Vishkambha", "Priti", "Ayushman", "Saubhagya", "Shobhana", "Atiganda", "Sukarma", "Dhriti",
         "Shula", "Ganda", "Vriddhi", "Dhruva", "Vyaghata", "Harshana", "Vajra", "Siddhi", "Vyatipata",
         "Variyana", "Parigha", "Shiva", "Siddha", "Sadhya", "Shubha", "Shukla", "Brahma", "Indra",
         "Vaidhriti"]
KARANAS = ["Bava", "Balava", "Kaulava", "Taitila", "Garaja", "Vanija", "Vishti"]
RASHIS = ["Mesha", "Vrishabha", "Mithuna", "Karka", "Simha", "Kanya", "Tula", "Vrischika", "Dhanu",
          "Makara", "Kumbha", "Meena"]
MASAS = ["Chaitra", "Vaishakha", "Jyeshtha", "Ashadha", "Shravana", "Bhadrapada", "Ashwija", "Kartika",
         "Margashira", "Pushya", "Magha", "Phalguna"]
RITUS = ["Vasanta", "Grishma", "Varsha", "Sharad", "Hemanta", "Shishira"]
VARAS = ["Somavara", "Mangalavara", "Budhavara", "Guruvara", "Shukravara", "Shanivara", "Bhanuvara"]
SAMVATSARAS = [
    "Prabhava", "Vibhava", "Shukla", "Pramoda", "Prajotpatti", "Angirasa", "Shrimukha", "Bhava", "Yuva",
    "Dhatu", "Ishvara", "Bahudhanya", "Pramathi", "Vikrama", "Vrisha", "Chitrabhanu", "Svabhanu", "Tarana",
    "Parthiva", "Vyaya", "Sarvajit", "Sarvadhari", "Virodhi", "Vikriti", "Khara", "Nandana", "Vijaya",
    "Jaya", "Manmatha", "Durmukhi", "Hevilambi", "Vilambi", "Vikari", "Sharvari", "Plava", "Shubhakrit",
    "Shobhakrit", "Krodhi", "Vishvavasu", "Parabhava", "Plavanga", "Kilaka", "Saumya", "Sadharana",
    "Virodhikrit", "Paridhavi", "Pramadicha", "Ananda", "Rakshasa", "Nala", "Pingala", "Kalayukti",
    "Siddharthi", "Raudri", "Durmati", "Dundubhi", "Rudhirodgari", "Raktakshi", "Krodhana", "Akshaya"]
# Eighths of the daytime (0 = the first), by weekday (Monday = 0).
RAHU = {0: 1, 1: 6, 2: 4, 3: 5, 4: 3, 5: 2, 6: 7}
YAMAGANDA = {0: 3, 1: 2, 2: 1, 3: 0, 4: 6, 5: 5, 6: 4}
GULIKA = {0: 5, 1: 4, 2: 3, 3: 2, 4: 1, 5: 0, 6: 6}

load = Loader(Path(__file__).with_name(".skyfield"))
ts = load.timescale()
eph = load("de421.bsp")
EARTH, SUN, MOON = eph["earth"], eph["sun"], eph["moon"]
HERE = wgs84.latlon(PLACE[1], PLACE[2])


# ---------------------------------------------------------------------------
# Astronomy
# ---------------------------------------------------------------------------
def ayanamsa(t):
    """Lahiri (Chitrapaksha) ayanamsa in degrees: 23.857092 at J2000, moving with the precession."""
    centuries = (t.tt - 2451545.0) / 36525
    return 23.857092 + (5028.796195 * centuries + 1.1054348 * centuries ** 2) / 3600


def sidereal(t):
    """Sidereal longitudes (degrees) of the Sun and the Moon, seen from the Earth's centre."""
    at = EARTH.at(t)
    sun = at.observe(SUN).apparent().frame_latlon(ecliptic_frame)[1].degrees
    moon = at.observe(MOON).apparent().frame_latlon(ecliptic_frame)[1].degrees
    shift = ayanamsa(t)
    return (sun - shift) % 360, (moon - shift) % 360


def element(fn, step: float):
    """A function of time giving a whole-number index, as almanac.find_discrete wants it."""
    def index(t):
        sun, moon = sidereal(t)
        return fn(sun, moon).astype(int)
    index.step_days = step
    return index


TITHI = element(lambda s, m: ((m - s) % 360) // 12, 0.25)
NAKSHATRA = element(lambda s, m: m // (360 / 27), 0.25)
YOGA = element(lambda s, m: ((s + m) % 360) // (360 / 27), 0.25)
KARANA = element(lambda s, m: ((m - s) % 360) // 6, 0.1)
MOON_RASHI = element(lambda s, m: m // 30, 0.25)


def karana_name(index: int) -> str:
    if index == 0:
        return "Kimstughna"
    if index >= 57:
        return ["Shakuni", "Chatushpada", "Naga"][index - 57]
    return KARANAS[(index - 1) % 7]


def tithi_name(index: int) -> str:
    paksha = "Shukla" if index < 15 else "Krishna"
    if index == 14:
        return "Purnima"
    if index == 29:
        return "Amavasya"
    return f"{paksha} {TITHIS[index % 15]}"


def changes(fn, start, end) -> tuple[int, list[tuple[datetime, int]]]:
    """The index at `start`, and each change (time, new index) until `end`."""
    first = int(fn(start))
    times, values = almanac.find_discrete(start, end, fn)
    return first, [(t.astimezone(IST), int(v)) for t, v in zip(times, values)]


def rise_set(body, day: date):
    """Rise and set times on the IST day, or None: the Sun's upper edge with refraction, the
    Moon's centre without (as Panchangs reckon them)."""
    start = ts.from_datetime(datetime.combine(day, datetime.min.time(), IST))
    horizon = {"horizon_degrees": 0.0} if body is MOON else {}
    times, _ = almanac.find_risings(EARTH + HERE, body, start, start + 1, **horizon)
    rise = times[0].astimezone(IST) if len(times) else None
    times, _ = almanac.find_settings(EARTH + HERE, body, start, start + 1, **horizon)
    sets = times[0].astimezone(IST) if len(times) else None
    return rise, sets


def masa(sunrise) -> tuple[str, int]:
    """The amanta lunar month: named from the Sun's sign at the new moon that began it
    (Adhika when no new sign starts before the next new moon)."""
    times, phases = almanac.find_discrete(sunrise - 32, sunrise + 32, almanac.moon_phases(eph))
    news = [t for t, p in zip(times, phases) if p == 0]
    before = [t for t in news if t.tt <= sunrise.tt][-1]
    after = next(t for t in news if t.tt > sunrise.tt)
    sign = int(sidereal(before)[0] // 30)
    adhika = sign == int(sidereal(after)[0] // 30)
    return ("Adhika " if adhika else "") + MASAS[(sign + 1) % 12], (sign + 1) % 12


def panchang(day: date) -> dict:
    rise, sets = rise_set(SUN, day)
    next_rise, _ = rise_set(SUN, day + timedelta(days=1))
    t_rise, t_next = ts.from_datetime(rise), ts.from_datetime(next_rise)
    out = {"date": day, "sunrise": rise, "sunset": sets}
    out["moonrise"], out["moonset"] = rise_set(MOON, day)
    for name, fn in (("tithi", TITHI), ("nakshatra", NAKSHATRA), ("yoga", YOGA), ("karana", KARANA),
                     ("moon_rashi", MOON_RASHI)):
        out[name] = changes(fn, t_rise, t_next)
    sun, moon = sidereal(t_rise)
    out["sun_rashi"] = RASHIS[int(sun // 30)]
    out["ayana"] = "Uttarayana" if int(sun // 30) in (9, 10, 11, 0, 1, 2) else "Dakshinayana"
    out["masa"], month = masa(t_rise)
    out["ritu"] = RITUS[month // 2]
    # Shalivahana Shaka year: it starts at Ugadi (Chaitra), so Jan-Apr before it is the previous year.
    shaka = day.year - 78 - (1 if day.month <= 4 and month >= 9 else 0)
    out["samvatsara"] = SAMVATSARAS[(shaka + 11) % 60]
    out["vara"] = VARAS[day.weekday()]
    elongation = (moon - sun) % 360
    out["moon_lit"] = (1 - math.cos(math.radians(elongation))) / 2
    out["waxing"] = elongation < 180
    eighth = (sets - rise) / 8
    out["rahu"] = (rise + eighth * RAHU[day.weekday()], rise + eighth * (RAHU[day.weekday()] + 1))
    out["yamaganda"] = (rise + eighth * YAMAGANDA[day.weekday()], rise + eighth * (YAMAGANDA[day.weekday()] + 1))
    out["gulika"] = (rise + eighth * GULIKA[day.weekday()], rise + eighth * (GULIKA[day.weekday()] + 1))
    muhurta = (sets - rise) / 15
    out["abhijit"] = (rise + muhurta * 7, rise + muhurta * 8)
    return out


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------
BG_TOP, BG_BOTTOM = (10, 14, 38), (30, 18, 62)
CREAM, MUTED, GOLD = (243, 238, 226), (170, 166, 198), (232, 196, 106)
RED, GREEN, LINE = (239, 140, 128), (143, 212, 164), (70, 66, 110)
FONTS = {
    False: ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "C:/Windows/Fonts/arial.ttf"],
    True: ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"],
}


def font(size: int, bold: bool = False):
    path = next((p for p in FONTS[bold] if Path(p).exists()), None)
    return ImageFont.truetype(path, size * SCALE) if path else ImageFont.load_default(size * SCALE)


def clock(moment: datetime, day: date) -> str:
    """Hours and minutes (seconds dropped, as Panchangs give the change times), with the
    weekday when not on `day`."""
    text = moment.strftime("%I:%M %p").lstrip("0")
    return text if moment.date() == day else f"{text} {moment.strftime('%a')}"


def rounded(moment: datetime | None, day: date) -> str:
    """To the nearest minute, as Panchangs give sunrise, sunset and the kaals."""
    return clock((moment + timedelta(seconds=30)).replace(second=0, microsecond=0), day) if moment else "—"


def span(pair, day) -> str:
    return f"{rounded(pair[0], day)} – {rounded(pair[1], day)}"


def until(found, names, day) -> tuple[str, str]:
    """The element's name at sunrise, and when it ends ("until 2:14 PM → Ekadashi")."""
    first, later = found
    if not later:
        return names(first), "all day"
    rest = " → ".join(names(v) for _, v in later)
    return names(first), f"until {clock(later[0][0], day)} → {rest}"


def draw_moon(draw, cx, cy, r, lit, waxing):
    s = SCALE
    cx, cy, r = cx * s, cy * s, r * s
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(44, 46, 78))
    side = 1 if waxing else -1
    limb = [(cx + side * r * math.cos(a), cy + r * math.sin(a))
            for a in (math.radians(d) for d in range(-90, 91, 2))]
    edge = [(cx + side * r * (1 - 2 * lit) * math.cos(a), cy + r * math.sin(a))
            for a in (math.radians(d) for d in range(90, -91, -2))]
    draw.polygon(limb + edge, fill=(240, 232, 205))


def render(p: dict, path: Path) -> None:
    w, h = SIZE[0] * SCALE, SIZE[1] * SCALE
    image = Image.new("RGB", (w, h))
    draw = ImageDraw.Draw(image)
    for y in range(h):  # vertical gradient
        f = y / h
        draw.line([(0, y), (w, y)], fill=tuple(int(a + (b - a) * f) for a, b in zip(BG_TOP, BG_BOTTOM)))
    day, x, half = p["date"], 80, SIZE[0] // 2 + 10

    def text(xy, value, size, color=CREAM, bold=False, anchor="la"):
        draw.text((xy[0] * SCALE, xy[1] * SCALE), value, font=font(size, bold), fill=color, anchor=anchor)

    def rule(y):
        draw.line([(x * SCALE, y * SCALE), ((SIZE[0] - x) * SCALE, y * SCALE)], fill=LINE, width=2 * SCALE)

    def grid(y, cells, color=CREAM):
        """Label and value pairs, two to a row."""
        for i, (label, value) in enumerate(cells):
            left = x if i % 2 == 0 else half
            top = y + (i // 2) * 100
            text((left, top), label.upper(), 24, MUTED)
            text((left, top + 32), value, 36, color, bold=True)
        return y + -(-len(cells) // 2) * 100

    y = TOP
    text((x, y), f"{day.strftime('%A, %d %B %Y')} · {p['vara']}", 32, MUTED)
    y += 50
    text((x, y), f"{p['samvatsara']} · {p['masa']} masa · {p['ritu']} ritu · {p['ayana']}", 28, MUTED)

    y += 84
    draw_moon(draw, x + 60, y + 60, 60, p["moon_lit"], p["waxing"])
    name, ends = until(p["tithi"], tithi_name, day)
    text((x + 156, y + 6), name, 58, CREAM, bold=True)
    text((x + 158, y + 84), ends, 30, GOLD)
    y += 168

    rule(y)
    y += 34
    for label, found, names in (("Nakshatra", p["nakshatra"], lambda i: NAKSHATRAS[i]),
                                ("Yoga", p["yoga"], lambda i: YOGAS[i]),
                                ("Karana", p["karana"], karana_name),
                                ("Moon in", p["moon_rashi"], lambda i: RASHIS[i])):
        name, ends = until(found, names, day)
        if label == "Moon in":
            ends += f" · Sun in {p['sun_rashi']}"
        text((x, y + 8), label.upper(), 24, MUTED)
        text((x + 240, y), name, 40, CREAM, bold=True)
        text((x + 240, y + 50), ends, 28, GOLD)
        y += 106

    rule(y)
    y += 34
    y = grid(y, [("Sunrise", rounded(p["sunrise"], day)), ("Sunset", rounded(p["sunset"], day)),
                 ("Moonrise", rounded(p["moonrise"], day)), ("Moonset", rounded(p["moonset"], day))])
    rule(y)
    y += 34
    y = grid(y, [("Rahu kaal", span(p["rahu"], day)), ("Yamaganda", span(p["yamaganda"], day)),
                 ("Gulika", span(p["gulika"], day))], RED)
    text((half, y - 100), "ABHIJIT", 24, MUTED)
    text((half, y - 68), span(p["abhijit"], day), 36, GREEN, bold=True)

    text((SIZE[0] // 2, y + 20), f"{PLACE[0]} · Lahiri ayanamsa · made {datetime.now(IST):%d %b, %I:%M %p}",
         22, MUTED, anchor="ma")
    image.resize(SIZE, Image.LANCZOS).save(path, optimize=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("out", type=Path)
    parser.add_argument("--date", type=date.fromisoformat, default=None, help="default: today in IST")
    args = parser.parse_args()
    day = args.date or datetime.now(IST).date()
    p = panchang(day)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    render(p, args.out)
    for key in ("tithi", "nakshatra", "yoga", "karana"):
        print(key, p[key])
    print({k: p[k] for k in ("sunrise", "sunset", "masa", "samvatsara", "ritu", "ayana", "sun_rashi")})


if __name__ == "__main__":
    main()
