#!/usr/bin/env python3
"""Build supabase/schema.sql, the clean load/*.csv files and load/ROW_COUNTS.md.

Run from the repo root:  python3 scripts/build_load_files.py

Tables come from two places:
  * sources/dashboard-data.json -- the only file the page renders from. Each metric
    section becomes its own table (lists of products get their own table), with the
    JSON keys as column names. Nested keys are joined with "_".
  * data/d10/*.csv -- one table per file, columns named exactly as the file's header.
    D10-10 (Yuhang PO history) is Restricted and is deliberately not loaded.

Every table also gets `source_row` (the row's 1-based position in its source), used
as the primary key and as the sort key when the page reads the table in pages.

The script stops with an error if any value would change on the way through.
"""
import csv
import datetime as dt
import json
import os
import re
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D10 = os.path.join(ROOT, "data", "d10")
LOAD = os.path.join(ROOT, "load")
JSON_PATH = os.path.join(ROOT, "sources", "dashboard-data.json")

MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


# ---------- value cleaners: each returns the clean string, or "" for NULL ----------
def c_text(v):
    return v


def c_int(v):
    s = v.strip().replace(",", "")
    if s == "":
        return ""
    if not re.fullmatch(r"-?\d+", s):
        raise ValueError("not an integer: %r" % v)
    return s


def c_num(v):
    s = v.strip().replace("$", "").replace(",", "").replace("%", "")
    if s == "":
        return ""
    if not re.fullmatch(r"-?\d+(\.\d+)?", s):
        raise ValueError("not a number: %r" % v)
    return s


def c_iso_date(v):
    s = v.strip()
    if s == "":
        return ""
    dt.date.fromisoformat(s)
    return s


def c_mon_year(v):          # "Jan 2025" -> 2025-01-01
    m, y = v.strip().split()
    return "%04d-%02d-01" % (int(y), MONTHS[m])


def c_mdy(v):               # "01/02/2025" (month/day/year) -> 2025-01-02
    m, d, y = v.strip().split("/")
    return dt.date(int(y), int(m), int(d)).isoformat()


def c_mon_d_y(v):           # "Jan 3, 2026" -> 2026-01-03
    return dt.datetime.strptime(v.strip(), "%b %d, %Y").date().isoformat()


def c_iso_ts(v):            # "2026-01-01 06:00" stays as is (date part already YYYY-MM-DD)
    s = v.strip()
    dt.datetime.strptime(s, "%Y-%m-%d %H:%M")
    return s


SQL_TYPE = {c_text: "text", c_int: "integer", c_num: "numeric", c_iso_date: "date",
            c_mon_year: "date", c_mdy: "date", c_mon_d_y: "date", c_iso_ts: "timestamp"}


# ---------- d10 files: table name, file, cleaner per column (in header order) ----------
T, I, N = c_text, c_int, c_num
D10_TABLES = [
    ("d10_01_order_ledger", "D10-01 Order Ledger 2025-2026.csv",
     [T, c_iso_date, c_iso_date, T, T, T, T, I, N, N, N, N, I, c_iso_date, N]),
    ("d10_02_revenue_by_month", "D10-02 Revenue by Month 2025-2026.csv",
     [c_mon_year, N, N, N, N, N, T]),
    ("d10_03_amazon_business_report", "D10-03 Amazon Business Report Jan 2025-Mar 2026.csv",
     [c_mon_year, T, T, T, I, N]),
    ("d10_04_amazon_refunds_by_order_date", "D10-04 Amazon Refunds by Order Date Jan 2025-Mar 2026.csv",
     [c_mon_year, T, T, T, I, N]),
    ("d10_05_shopify_sales_by_product_weekly", "D10-05 Shopify Sales by Product Weekly Jan 2025-Mar 2026.csv",
     [c_iso_date, T, T, I, N, N, N, N]),
    ("d10_06a_sales_history_amazon_units_ordered", "D10-06a Sales History - Amazon units ordered.csv",
     [c_iso_date] + [I] * 12),
    ("d10_06b_sales_history_amazon_units_refunded", "D10-06b Sales History - Amazon units refunded.csv",
     [c_iso_date] + [I] * 12),
    ("d10_06c_sales_history_shopify_net_quantity", "D10-06c Sales History - Shopify net quantity.csv",
     [c_mdy] + [I] * 13),
    ("d10_07_3pl_daily_stock", "D10-07 3PL Daily Stock Jan-Mar 2026.csv",
     [c_iso_ts, T, T, T, I, I, I, I, I, c_iso_date, T]),
    ("d10_08_bidworks_monthly_report", "D10-08 BidWorks Monthly Report Jan 2025-Mar 2026.csv",
     [c_mon_year, N, I, I, N, N, N]),
    # "Month" here is "Jan"/"Feb"/"Mar" with no year, so it stays text; "Date issued" is the real date.
    ("d10_09_bidworks_invoices", "D10-09 BidWorks Invoices 2026.csv",
     [T, T, c_mon_d_y, T, N, T, T]),
]
# SKU crosswalk between the three naming schemes (D10-07 item_code, D10-06a/b Amazon column,
# D10-06c Shopify column). No source file holds this mapping; these 12 rows were confirmed by
# the dashboard owner on 13 Oct 2026. Griddle Plate is deliberately absent (Kickstarter-only,
# not in D10-07 or the Amazon history).
SKU_CROSSWALK_SOURCE = "Confirmed SKU crosswalk (dashboard owner, 13 Oct 2026); checked against D10-06a/b/c headers and D10-07 item codes"
SKU_CROSSWALK = [
    ("TNHC-1001", "TRAILHEAD-GRILL-BLK", "trailhead-portable-grill"),
    ("TNHC-1002", "TRAILHEAD-GRILL-PRO-V2", "trailhead-grill-pro"),
    ("TNHC-2010", "CI-SKILLET-10IN", "cast-iron-skillet-10"),
    ("TNHC-2006", "CI-DUTCHOVEN-6QT", "cast-iron-dutch-oven-6qt"),
    ("TNHC-3001", "CAMPKIT-DELUXE", "camp-kitchen-kit-deluxe"),
    ("TNHC-3002", "CAMPKIT-BASIC", "camp-kitchen-kit-basic"),
    ("TNHC-3008", "NEST-COOKWARE-8PC", "nesting-cookware-set-8pc"),
    ("TNHC-4001", "ACC-WINDSHIELD-SET", "windshield-panel-set"),
    ("TNHC-4002", "ACC-PREPTABLE-FOLD", "folding-prep-table"),
    ("TNHC-4003", "ACC-COFFEEPRESS-INS", "insulated-coffee-press"),
    ("TNHC-4004", "ACC-FIRETOOL-SET", "fire-poker-tool-set"),
    ("TNHC-4005", "ACC-GRATE-CLEANER", "grill-grate-cleaner-kit"),
]

EXCLUDED = [("D10-10 Yuhang PO History to 31 Mar 2026.csv",
             "Restricted per the Day 1 data classification log; not used by any metric")]


def read_d10(fname, cleaners):
    """Return (header, rows, removed) with title/blank/note/total lines dropped."""
    with open(os.path.join(D10, fname), newline="", encoding="utf-8-sig") as fh:
        lines = list(csv.reader(fh))
    hi = next(i for i, r in enumerate(lines) if sum(1 for c in r if c.strip()) > 2)
    header = lines[hi]
    assert len(header) == len(cleaners), (fname, len(header), len(cleaners))
    removed = [("title", " ".join(c for c in r if c)) for r in lines[:hi] if any(c.strip() for c in r)]
    rows = []
    for r in lines[hi + 1:]:
        if not any(c.strip() for c in r):
            continue                                   # blank line
        if len(r) != len(header):
            removed.append(("note", " ".join(r)))
            continue
        if r[0].startswith("Total"):
            removed.append(("total", r[0]))
            continue
        rows.append(r)
    return header, rows, removed


def check_same(raw, clean, cleaner):
    """Prove the clean value means exactly the raw value."""
    if clean == "":
        assert raw.strip() == "", raw
        return
    if cleaner in (c_int, c_num):
        assert Decimal(clean) == Decimal(raw.strip().replace("$", "").replace(",", "").replace("%", "")), (raw, clean)
    elif cleaner is c_text:
        assert clean == raw


# ---------- dashboard-data.json tables ----------
def dashboard_tables(d):
    """Split dashboard-data.json into tables. Returns list of (name, json_path, cols, rows)."""
    out = []
    out.append(("dashboard_data_notes", "data_notes", [("note", c_text)],
                [[n] for n in d["data_notes"]]))

    r = d["revenue_vs_last_year"]
    ly = r["last_year_same_month"]
    out.append(("dashboard_revenue_vs_last_year", "revenue_vs_last_year",
                [("as_of", T), ("current_month_value", N), ("current_month_label", T),
                 ("last_year_same_month_month", c_iso_date), ("last_year_same_month_amazon", N),
                 ("last_year_same_month_shopify", N), ("last_year_same_month_total", N),
                 ("source", T), ("scope", T)],
                [[r["as_of"], r["current_month_value"], r["current_month_label"],
                  ly["month"] + "-01", ly["amazon"], ly["shopify"], ly["total"], r["source"], r["scope"]]]))

    s = d["weeks_of_stock_cover"]
    out.append(("dashboard_weeks_of_stock_cover", "weeks_of_stock_cover",
                [("as_of", T), ("flag_under_weeks", I), ("source", T)],
                [[s["as_of"], s["flag_under_weeks"], s["source"]]]))
    out.append(("dashboard_weeks_of_stock_cover_products", "weeks_of_stock_cover.products",
                [("sku", T), ("name", T), ("on_hand", I), ("weekly_rate", N), ("weeks_cover", N), ("note", T)],
                [[p["sku"], p["name"], p["on_hand"], p["weekly_rate"], p["weeks_cover"], p.get("note")]
                 for p in s["products"]]))

    pa = d["profit_after_ads"]
    out.append(("dashboard_profit_after_ads", "profit_after_ads",
                [("as_of", T), ("source", T)], [[pa["as_of"], pa["source"]]]))
    out.append(("dashboard_profit_after_ads_products", "profit_after_ads.products",
                [("sku", T), ("name", T), ("net_contribution_after_ads", N)],
                [[p["sku"], p["name"], p["net_contribution_after_ads"]] for p in pa["products"]]))

    c = d["cash_in_bank"]
    out.append(("dashboard_cash_in_bank", "cash_in_bank",
                [("as_of", T), ("value", N), ("status", T), ("note", T)],
                [[c["as_of"], c["value"], c["status"], c["note"]]]))
    return out


def json_cell(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        raise ValueError("unexpected boolean")
    if isinstance(v, (int, float)):
        return json.dumps(v)           # 28.5 -> "28.5", 0.0 -> "0.0", -19525 -> "-19525"
    return v


def rebuild_from_csv(tables_rows):
    """Inverse of dashboard_tables(): what the page will rebuild from the tables."""
    def num(s):
        return None if s == "" else (int(s) if re.fullmatch(r"-?\d+", s) else float(s))

    def txt(s):
        return None if s == "" else s

    t = tables_rows
    rv = t["dashboard_revenue_vs_last_year"][0]
    sc = t["dashboard_weeks_of_stock_cover"][0]
    pa = t["dashboard_profit_after_ads"][0]
    ca = t["dashboard_cash_in_bank"][0]
    prods = []
    for r in t["dashboard_weeks_of_stock_cover_products"]:
        p = {"sku": r[1], "name": r[2], "on_hand": num(r[3]), "weekly_rate": num(r[4]), "weeks_cover": num(r[5])}
        if r[6] != "":
            p["note"] = r[6]
        prods.append(p)
    return {
        "data_notes": [r[1] for r in t["dashboard_data_notes"]],
        "revenue_vs_last_year": {
            "as_of": txt(rv[1]), "current_month_value": num(rv[2]), "current_month_label": txt(rv[3]),
            "last_year_same_month": {"month": rv[4][:7], "amazon": num(rv[5]), "shopify": num(rv[6]),
                                     "total": num(rv[7])},
            "source": txt(rv[8]), "scope": txt(rv[9])},
        "weeks_of_stock_cover": {"as_of": txt(sc[1]), "flag_under_weeks": num(sc[2]), "products": prods,
                                 "source": txt(sc[3])},
        "profit_after_ads": {"as_of": txt(pa[1]), "products": [
            {"sku": r[1], "name": r[2], "net_contribution_after_ads": num(r[3])}
            for r in t["dashboard_profit_after_ads_products"]], "source": txt(pa[2])},
        "cash_in_bank": {"as_of": txt(ca[1]), "value": num(ca[2]), "status": txt(ca[3]), "note": txt(ca[4])},
    }


def qi(name):
    return '"' + name.replace('"', '""') + '"'


def main():
    os.makedirs(LOAD, exist_ok=True)
    os.makedirs(os.path.join(ROOT, "supabase"), exist_ok=True)
    tables = []      # (name, load_file, source_desc, [(col, cleaner)], clean_rows, removed)

    with open(JSON_PATH, encoding="utf-8") as fh:
        dash = json.load(fh)
    for name, path, cols, rows in dashboard_tables(dash):
        clean = [[json_cell(v) for v in r] for r in rows]
        for r in clean:
            for (col, cl), v in zip(cols, r):
                if v != "":
                    cl(v)
        tables.append((name, "sources/dashboard-data.json → " + path, cols, clean, []))

    for name, fname, cleaners in D10_TABLES:
        header, rows, removed = read_d10(fname, cleaners)
        clean = []
        for r in rows:
            out = []
            for raw, cl in zip(r, cleaners):
                v = cl(raw)
                check_same(raw, v, cl)
                out.append(v)
            clean.append(out)
        tables.append((name, "data/d10/" + fname, list(zip(header, cleaners)), clean, removed))

    # SKU crosswalk: every value must exist in the files it points into.
    hdr = {}
    for fname in ("D10-06a Sales History - Amazon units ordered.csv", "D10-06b Sales History - Amazon units refunded.csv",
                  "D10-06c Sales History - Shopify net quantity.csv"):
        with open(os.path.join(D10, fname), newline="", encoding="utf-8-sig") as fh:
            hdr[fname[:7]] = next(csv.reader(fh))
    with open(os.path.join(D10, "D10-07 3PL Daily Stock Jan-Mar 2026.csv"), newline="", encoding="utf-8-sig") as fh:
        item_codes = {r["item_code"] for r in csv.DictReader(fh)}
    assert len({r[0] for r in SKU_CROSSWALK}) == len(SKU_CROSSWALK) == len(item_codes), "crosswalk must cover every D10-07 item once"
    for code, amazon, shopify in SKU_CROSSWALK:
        assert code in item_codes, code
        assert amazon in hdr["D10-06a"] and amazon in hdr["D10-06b"], amazon
        assert shopify in hdr["D10-06c"], shopify
    tables.append(("d10_sku_crosswalk", SKU_CROSSWALK_SOURCE,
                   [("item_code", T), ("amazon_column", T), ("shopify_column", T)],
                   [list(r) for r in SKU_CROSSWALK], []))

    # Column names must survive Postgres' 63-byte identifier limit unchanged.
    for name, _, cols, _, _ in tables:
        assert len(name.encode()) <= 63, name
        for col, _ in cols:
            assert len(col.encode()) <= 63, (name, col)

    # ---- write load CSVs (header = table columns exactly) ----
    by_name = {}
    for name, _, cols, rows, _ in tables:
        path = os.path.join(LOAD, name + ".csv")
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh, lineterminator="\n")
            w.writerow(["source_row"] + [c for c, _ in cols])
            for i, r in enumerate(rows, 1):
                w.writerow([i] + r)
        with open(path, newline="", encoding="utf-8") as fh:
            back = list(csv.reader(fh))
        by_name[name] = back[1:]
        assert back[0] == ["source_row"] + [c for c, _ in cols]
        assert len(back) - 1 == len(rows)

    # ---- checks that no number changed ----
    assert rebuild_from_csv(by_name) == dash, "dashboard tables do not rebuild dashboard-data.json"

    rev = by_name["d10_02_revenue_by_month"]
    with open(os.path.join(D10, "D10-02 Revenue by Month 2025-2026.csv"), newline="", encoding="utf-8-sig") as fh:
        totals = {r[0]: r for r in csv.reader(fh) if r and r[0].startswith("Total")}
    for label, prefix in (("Total 2025", "2025-"), ("Total 2026 (Jan-Mar)", "2026-")):
        for j in range(2, 7):
            s = sum(Decimal(r[j]) for r in rev if r[1].startswith(prefix))
            assert s == Decimal(totals[label][j - 1]), (label, j, s, totals[label][j - 1])

    days = [r[1] for r in by_name["d10_06c_sales_history_shopify_net_quantity"]]
    for a, b in zip(days, days[1:]):
        assert dt.date.fromisoformat(b) - dt.date.fromisoformat(a) == dt.timedelta(days=1), (a, b)

    # ---- schema.sql ----
    sql = ["-- Tannerhill dashboard tables for Supabase.",
           "-- Generated by scripts/build_load_files.py -- edit that script, not this file.",
           "-- Every table: row level security on, anon may SELECT all rows, nothing else.",
           "-- D10-10 (Yuhang PO history) is intentionally absent: Restricted data.",
           "", "begin;", ""]
    for name, src, cols, _, _ in tables:
        t = "public." + qi(name)
        sql.append("-- Source: " + src)
        sql.append("create table if not exists %s (" % t)
        defs = ["  source_row integer primary key"] + ["  %s %s" % (qi(c), SQL_TYPE[cl]) for c, cl in cols]
        sql.append(",\n".join(defs))
        sql.append(");")
        sql.append("alter table %s enable row level security;" % t)
        sql.append("revoke all on table %s from anon;" % t)
        sql.append("grant select on table %s to anon;" % t)
        sql.append('drop policy if exists "anon can read all rows" on %s;' % t)
        sql.append('create policy "anon can read all rows" on %s for select to anon using (true);' % t)
        sql.append("")
    sql.append("commit;")
    with open(os.path.join(ROOT, "supabase", "schema.sql"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(sql) + "\n")

    # ---- ROW_COUNTS.md ----
    md = ["# Row counts", "",
          "Generated by `scripts/build_load_files.py`. Data rows only: the header line is not counted.", "",
          "| Table | Load file | Source file | Data rows |", "|---|---|---|---:|"]
    for name, src, _, rows, _ in tables:
        md.append("| `%s` | `load/%s.csv` | `%s` | %s |" % (name, name, src, format(len(rows), ",")))
    md += ["", "Total data rows: %s across %d tables." % (format(sum(len(t[3]) for t in tables), ","), len(tables)),
           "", "## Not loaded", ""]
    for f, why in EXCLUDED:
        md.append("- `data/d10/%s`: %s." % (f, why))
    md += ["", "## Lines removed from the source files", ""]
    any_removed = False
    for name, src, _, _, removed in tables:
        for kind, text in removed:
            any_removed = True
            md.append("- `%s` (%s): %s" % (src.replace("data/d10/", ""), kind, text if len(text) < 90 else text[:87] + "..."))
    if not any_removed:
        md.append("- None.")
    md += ["", "## Checks the script ran", "",
           "- Every number in every load file equals its source cell once `$`, `,` and `%` are removed.",
           "- The seven `dashboard_*` tables rebuild into exactly `sources/dashboard-data.json`.",
           "- D10-02: each column of the 2025 and Jan-Mar 2026 monthly rows adds up to the file's own Total row.",
           "- D10-06c: dates read as month/day/year run one day apart from 2025-01-01 to 2026-03-31."]
    with open(os.path.join(LOAD, "ROW_COUNTS.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(md) + "\n")

    for name, _, _, rows, _ in tables:
        print("%-46s %7d rows" % (name, len(rows)))


if __name__ == "__main__":
    main()
