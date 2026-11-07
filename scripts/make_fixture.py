"""Generate the SYNTHETIC Evaboot-style export and the draft golden set. Seeded and deterministic.

Column names follow the Evaboot API example keys ("Full Name", "Email", "Email Status",
"Company Name", "Current Job"); the rest are best guesses and are mapped in one place
(sdr_loop.steps.extract.EVABOOT_COLUMNS). Replace fixtures/evaboot_export.csv with a real
export when available. Golden labels are DRAFT labels: review them by hand (PRD day 1).
"""

from __future__ import annotations

import csv
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
rnd = random.Random(7)

FIRST = "Alex Sam Jordan Maya Liam Noah Emma Lea Hugo Chloe Marco Anna Tom Nina Paul Sofia Ben Ines Lucas Eva".split()
LAST = "Martin Dupont Smith Garcia Muller Rossi Jansen Brown Moreau Wilson Weber Lopez Clark Petit Novak".split()
PREFIX = "Bright North Pulse Signal Nimbus Atlas Vector Oak Lumen Harbor Quanta Echo Summit Cobalt Mosaic".split()
SUFFIX_FIT = ["Reach", "Leads", "Outbound", "Growth", "Talent", "Search", "Pipeline", "Connect"]
SUFFIX_OTHER = ["Foods", "Steel", "Builders", "Motors", "Textiles", "Logistics", "Dental", "Farms"]

FIT_TITLES = [
    "Founder",
    "CEO",
    "Head of Growth",
    "Sales Manager",
    "Head of Outbound",
    "SDR Manager",
    "Business Development Manager",
    "Lead Generation Manager",
    "Agency Owner",
    "Talent Acquisition Lead",
]
FIT_INDUSTRIES = [
    "Marketing & Advertising",
    "Staffing & Recruiting",
    "Outsourcing/Offshoring",
    "Business Consulting & Services",
]
FIT_COUNTRIES = ["US", "GB", "FR", "DE", "CA", "NL", "ES", "IE"]
OFF_COUNTRIES = ["IN", "BR", "NG", "PK", "VN"]
OTHER_INDUSTRIES = [
    "Food & Beverages",
    "Construction",
    "Mining & Metals",
    "Automotive",
    "Retail",
    "Hospitality",
]
BAD_TITLES = [
    "Software Engineer",
    "Marketing Intern",
    "Student",
    "Executive Assistant",
    "Backend Developer",
]
COMPETITORS = ["Lemlist", "Waalaxy", "Apollo", "Skrapp"]

COLUMNS = [
    "Full Name",
    "First Name",
    "Last Name",
    "Current Job",
    "Company Name",
    "Company Website",
    "Industry",
    "Headcount",
    "Country",
    "LinkedIn Profile URL",
    "Email",
    "Email Status",
]

_used: set[str] = set()


def slug(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum())


def person() -> tuple[str, str]:
    return rnd.choice(FIRST), rnd.choice(LAST)


def make(kind: str) -> dict[str, str]:
    while True:
        first, last = person()
        company_base = f"{rnd.choice(PREFIX)} {rnd.choice(SUFFIX_FIT if kind not in ('wrong_industry',) else SUFFIX_OTHER)}"
        key = slug(first + last + company_base)
        if key not in _used:
            _used.add(key)
            break
    company = company_base
    domain = f"{slug(company)}.example"
    row = {
        "Full Name": f"{first} {last}",
        "First Name": first,
        "Last Name": last,
        "Current Job": rnd.choice(FIT_TITLES),
        "Company Name": company,
        "Company Website": f"https://{domain}",
        "Industry": rnd.choice(FIT_INDUSTRIES),
        "Headcount": str(rnd.choice([4, 8, 12, 25, 40, 75, 120, 250, 400])),
        "Country": rnd.choice(FIT_COUNTRIES),
        "LinkedIn Profile URL": f"https://www.linkedin.com/in/{slug(first)}-{slug(last)}-{rnd.randrange(16**6):06x}",
        "Email": f"{slug(first)}.{slug(last)}@{domain}",
        "Email Status": "safe",
    }
    # Hard cases: code rules fire (country / headcount / title / competitor / email).
    if kind == "wrong_country":
        row["Country"] = rnd.choice(OFF_COUNTRIES)
    elif kind == "too_big":
        row["Headcount"] = str(rnd.choice([1200, 5000, 30000]))
    elif kind == "too_small":
        row["Headcount"] = "1"
    elif kind == "bad_title":
        row["Current Job"] = rnd.choice(BAD_TITLES)
    elif kind == "competitor":
        row["Company Name"] = rnd.choice(COMPETITORS)
        row["Company Website"] = f"https://{slug(row['Company Name'])}.example"
    elif kind == "wrong_industry":  # passes code rules, LLM must reject
        row["Industry"] = rnd.choice(OTHER_INDUSTRIES)
    elif kind == "no_email":
        row["Email"], row["Email Status"] = "", ""
    elif kind == "bad_email":
        row["Email Status"] = rnd.choice(["riskier", "suspect", "invalid"])
    return row


MIX = {
    "fit": 130,
    "wrong_country": 20,
    "too_big": 18,
    "too_small": 8,
    "bad_title": 22,
    "competitor": 10,
    "wrong_industry": 52,
    "no_email": 20,
    "bad_email": 20,
}

rows: list[tuple[str, dict[str, str]]] = []
for kind, n in MIX.items():
    rows += [(kind, make(kind)) for _ in range(n)]
rnd.shuffle(rows)
# A few duplicates (same profile, trailing slash) to exercise dedup.
dups = [dict(r) for _, r in rnd.sample(rows, 6)]
for d in dups:
    d["LinkedIn Profile URL"] += "/"

(ROOT / "fixtures").mkdir(exist_ok=True)
with (ROOT / "fixtures/evaboot_export.csv").open("w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=COLUMNS)
    w.writeheader()
    for _, r in rows:
        w.writerow(r)
    for d in dups:
        w.writerow(d)

# Draft golden set: 50 leads with verified email, balanced. fit = kind "fit"; the rest not_fit.
verified = [(k, r) for k, r in rows if k not in ("no_email", "bad_email")]
fit = [x for x in verified if x[0] == "fit"][:22]
nonfit = [x for x in verified if x[0] != "fit"]
by_kind: dict[str, list[tuple[str, dict[str, str]]]] = {}
for k, r in nonfit:
    by_kind.setdefault(k, []).append((k, r))
gold_non: list[tuple[str, dict[str, str]]] = []
quota = {
    "wrong_industry": 12,
    "wrong_country": 4,
    "too_big": 3,
    "bad_title": 4,
    "competitor": 2,
    "too_small": 1,
}
for k, n in quota.items():
    gold_non += by_kind[k][:n]
with (ROOT / "evals/golden_leads.jsonl").open("w") as f:
    for k, r in fit + gold_non:
        f.write(
            json.dumps({"lead": r, "label": "fit" if k == "fit" else "not_fit", "archetype": k})
            + "\n"
        )
print(f"rows={len(rows)} dups={len(dups)} golden={len(fit) + len(gold_non)}")
