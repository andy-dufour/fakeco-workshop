"""Create the schema and the starting data: python -m fakeco.seed [--reset]. Safe to run again: it does nothing when the data is there."""
import argparse, datetime as dt, hashlib, pathlib, random, sys

import psycopg
from psycopg.types.json import Jsonb

from . import config
from .stories import FAULTS, STORIES

SCHEMA_VERSION = 1
FIRST = ["Dana", "Marcus", "Priya", "Jordan", "Elena", "Tomás", "Aisha", "Wei", "Chloe", "Omar", "Ingrid", "Sam", "Noor", "Luca", "Hannah", "Diego", "Mei", "Rafael", "Grace", "Yusuf",
         "Freya", "Andre", "Keiko", "Nadia"]
LAST = ["Ruiz", "Okafor", "Shah", "Lindqvist", "Moreau", "Park", "Hassan", "Brennan", "Castillo", "Novak", "Adeyemi", "Tanaka", "Kowalski", "Reyes", "Bergström", "Mitchell", "Varga",
        "Sinclair", "Haddad", "Whitaker"]
STAFF = [("SUP-ava", "Ava Chen", "support"), ("SUP-ben", "Ben Ortiz", "support"), ("OPS-kai", "Kai Jensen", "operations"), ("OPS-mia", "Mia Rossi", "operations"),
         ("CMP-lee", "Lee Tran", "compliance"), ("SYS-loadgen", "Load generator", "operations")]
# the instruments the telemetry generator trades (concierge/catalog.py SYMBOLS, backoffice.py CLASS_SYMBOLS)
NAMED = [("FXA-EQ", "Foxglove Aerospace", "Stocks"), ("NVD-EQ", "Novadyne Corp", "Stocks"), ("ACM-EQ", "Acme Industries", "Stocks"), ("ORB-EQ", "Orbital Labs", "Stocks"),
         ("SPY-IX", "Broad Market Index Fund", "Stocks"), ("BNDX", "Global Bond Fund", "Bonds"), ("TLT-BD", "Long Treasury Bond Fund", "Bonds"), ("AGG-BD", "Aggregate Bond Fund", "Bonds"),
         ("VNQ-RE", "Real Estate Index Fund", "Real Estate"), ("GLD-CM", "Gold Trust", "Commodities"), ("MMF-MM", "Money Market Fund", "Cash")]
WORDS = ["Northwind", "Harbor", "Summit", "Cedar", "Atlas", "Meridian", "Ironbridge", "Bluefin", "Oakridge", "Lumen", "Granite", "Pioneer", "Sterling", "Evergreen", "Crescent", "Redwood",
         "Tidewater", "Kestrel", "Foxglove", "Juniper"]
KINDS = {"Stocks": ("EQ", ["Holdings", "Systems", "Industries", "Labs", "Energy", "Foods"], (15, 420), 78), "Bonds": ("BD", ["Treasury 5Y", "Corporate Bond", "Municipal Bond", "Bond Fund"], (82, 108), 27),
         "Real Estate": ("RE", ["REIT", "Property Trust", "Realty Income"], (25, 160), 14), "Commodities": ("CM", ["Metals Trust", "Energy Fund", "Agriculture Fund"], (18, 210), 14),
         "Cash": ("MM", ["Cash Reserve", "Treasury Money Market"], (1, 1), 5)}
HISTORY_DAYS = 420
PLANS = {"bronze": .3, "silver": .45, "gold": .25}
HEAVY_LOTS = (8_800, 9_800)   # the one Bronze customer whose dividend reinvestment opened thousands of tax lots (fault settlement_lot_cache_leak)
CLASS_VOL = {"Stocks": .013, "Real Estate": .011, "Commodities": .012, "Bonds": .004, "Cash": 0.0}


def instruments(r):
    out, used, names = [], {s for s, _, _ in NAMED}, {n for _, n, _ in NAMED}
    for sym, name, cls in NAMED:
        price = 1.0 if cls == "Cash" else 12 + int(hashlib.md5(sym.encode()).hexdigest()[:6], 16) % 250  # as concierge/customers.price_of
        out.append((sym, name, cls, float(price)))
    for cls, (suffix, kinds, (lo, hi), n) in KINDS.items():
        made = 0
        while made < n:
            sym = "".join(r.choice("ABCDEFGHJKLNPQRSTUVWXYZ") for _ in range(r.choice([3, 4]))) + "-" + suffix
            name = f"{r.choice(WORDS)} {r.choice(kinds)}"
            if sym in used or name in names:
                continue
            used.add(sym); names.add(name); made += 1
            out.append((sym, name, cls, round(r.uniform(lo, hi), 2)))
    return out


def history(r, instr, today):
    """HISTORY_DAYS weekday closes per instrument, a random walk back from today's price."""
    rows = []
    for sym, _, cls, price in instr:
        d, p, n = today, price, 0
        while n < HISTORY_DAYS:
            if d.weekday() < 5:
                rows.append((sym, d, round(p, 2), r.randint(20_000, 9_000_000)))
                p *= r.gauss(1, CLASS_VOL[cls])
                n += 1
            d -= dt.timedelta(days=1)
    return rows


def plans(n=60):
    """Each customer's plan and open tax lots: [(plan, tax_lots)]. Its own random source, so the rest of the seed stays the same. The whales are Gold;
    exactly one Bronze customer has thousands of lots, everyone else a few hundred at most."""
    r = random.Random(23)
    out = [("gold" if i >= 58 else r.choices(list(PLANS), list(PLANS.values()))[0], max(8, round(r.lognormvariate(4.6, .7)))) for i in range(1, n + 1)]
    heavy = r.choice([i for i, (plan, _) in enumerate(out) if plan == "bronze"])
    out[heavy] = ("bronze", r.randint(*HEAVY_LOTS))
    return out


def seed(conn, today=None):
    r = random.Random(7)
    today = today or dt.date.today()
    cur = conn.cursor()
    cur.execute(pathlib.Path(__file__).with_name("schema.sql").read_text())
    instr = instruments(r)
    cur.executemany("INSERT INTO instruments VALUES (%s, %s, %s, %s)", instr)
    with cur.copy("COPY price_history (symbol, trade_date, close, volume) FROM STDIN") as cp:
        for row in history(r, instr, today):
            cp.write_row(row)
    by_class = {}
    for sym, _, cls, price in instr:
        by_class.setdefault(cls, []).append((sym, price))
    names = r.sample([(f, l) for f in FIRST for l in LAST], 60)
    asset_n, plan_lots = 0, plans(len(names))
    for i, (first, last) in enumerate(names, 1):
        plan, lots = plan_lots[i - 1]
        cid, aid, whale = f"CUS-{i:04d}", f"ACC-{i:04d}", i >= 58
        email = f"{first}.{last}@example.com".lower().replace("á", "a").replace("ö", "o")
        cur.execute("INSERT INTO customers (id, name, first_name, email, whale, funded, wallet_floor, wallet_target, app_version, plan) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (cid, f"{first} {last}", first, email, whale, r.random() < .35 or whale, r.choice([50, 100, 200]), r.choice([500, 1000, 2000]),
                     r.choices(["0.1.0-demo", "0.1.1-demo", "0.2.0-demo"], [1, 2, 4])[0], plan))
        cur.execute("INSERT INTO accounts VALUES (%s, %s, %s, %s, %s, %s)", (aid, cid, round(r.lognormvariate(9.2 if not whale else 11.5, .8), 2), round(r.lognormvariate(5.7, .9), 2),
                                                                         round(r.lognormvariate(6, 1.3), 2), lots))
        # a target mix of asset classes, then positions drawn to match it
        stocks, cash = r.randint(30, 70), r.randint(3, 10)
        bonds = r.randint(8, max(8, min(35, 90 - stocks - cash)))
        rest = 100 - stocks - bonds - cash
        mix = {"Stocks": stocks, "Bonds": bonds, "Cash": cash, "Real Estate": round(rest * .6), "Commodities": rest - round(rest * .6)}
        total = r.lognormvariate(11.3 if not whale else 14.2, .5)
        count = r.randint(105, 140) if whale else r.randint(6, 18)
        picks = []
        for cls, pct in mix.items():
            k = max(1, min(len(by_class[cls]), round(count * pct / 100) if cls != "Cash" else 1))
            picks += [(cls, sym, price, total * pct / 100 / k) for sym, price in r.sample(by_class[cls], k)]
        for cls, sym, price, value in picks:
            asset_n += 1
            qty = max(1, round(value * r.uniform(.6, 1.4) / price))
            cur.execute("INSERT INTO holdings VALUES (%s, %s, %s, %s)", (f"AST-{asset_n:06d}", aid, sym, qty))
        for _ in range(r.choice([0, 1, 2, 2, 3])):
            sym, price = r.choice(by_class["Stocks"][:5] + by_class["Bonds"][:3] + by_class["Real Estate"][:1])
            cur.execute("INSERT INTO trades (id, account_id, customer_id, side, symbol, quantity) VALUES ('TRD-' || lpad(nextval('trade_seq')::text, 8, '0'), %s, %s, %s, %s, %s)",
                        (aid, cid, r.choice(["buy", "sell"]), sym, r.choice([5, 10, 15, 20, 25, 40, 50])))
    for sid, name, role in STAFF:
        cur.execute("INSERT INTO staff VALUES (%s, %s, %s, %s, %s)", (sid, name, role, f"{sid.lower()}@fakecosecurities.example.com", sid.startswith("SYS-")))
    for name, (desc, params, _) in FAULTS.items():
        cur.execute("INSERT INTO faults (name, description, params, default_params) VALUES (%s, %s, %s, %s)", (name, desc, Jsonb(params), Jsonb(params)))
    for pos, (name, (title, kind, desc, parts)) in enumerate(STORIES.items()):
        cur.execute("INSERT INTO stories (name, title, description, kind, position, parts) VALUES (%s, %s, %s, %s, %s, %s)", (name, title, desc, kind, pos, Jsonb(parts)))
    cur.execute("INSERT INTO restricted_list VALUES ('ORB-EQ', 'Pending tender offer: no customer orders until it closes.', 'CMP-lee')")
    for key, value in {"loadgen": {"enabled": True, "conversations_per_minute": 6, "transactions_per_minute": 4, "auto_stories": False},
                       "loadgen_stats": {"conversations": 0, "turns": 0, "transactions": 0, "errors": 0, "last_seen_at": None},
                       "latest_app_version": "0.2.0-demo", "schema_version": SCHEMA_VERSION}.items():
        cur.execute("INSERT INTO settings VALUES (%s, %s)", (key, Jsonb(value)))   # schema_version goes in last: the services wait for it
    conn.commit()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reset", action="store_true", help="drop everything first")
    a = ap.parse_args(argv)
    with psycopg.connect(config.database_url()) as conn:
        if a.reset:
            conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            conn.commit()
        if conn.execute("SELECT to_regclass('settings')").fetchone()[0]:
            if conn.execute("SELECT 1 FROM settings WHERE key = 'schema_version'").fetchone():
                print("already seeded")
                return
            sys.exit("the database holds a half-made schema; run with --reset")
        seed(conn)
        print("seeded:", conn.execute("SELECT (SELECT count(*) FROM customers), (SELECT count(*) FROM instruments), (SELECT count(*) FROM holdings), (SELECT count(*) FROM price_history)").fetchone())


if __name__ == "__main__":
    main()
