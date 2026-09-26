"""Static configuration: paths, airports, accounts, default deal thresholds."""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "seatwatch"
# Runtime data lives outside ~/Documents: macOS privacy protection blocks
# background agents from Documents, and iCloud sync can corrupt SQLite.
DATA_DIR = Path(os.environ.get("SEATWATCH_DATA", Path.home() / "Library/Application Support/seatwatch"))
DB_PATH = DATA_DIR / "seatwatch.db"
LOG_DIR = DATA_DIR / "logs"
KEY_PATH = Path(os.environ.get("SEATS_AERO_KEY_FILE", Path.home() / ".config/seats-aero/key"))
PKG_DIR = Path(__file__).resolve().parent

HOST = "127.0.0.1"
PORT = int(os.environ.get("SEATWATCH_PORT", "8765"))

API_BASE = "https://seats.aero/partnerapi/"
DAILY_LIMIT = 1000
HARD_FLOOR = 3  # never let background work take the quota below this

CABINS = {"economy": "Y", "premium": "W", "business": "J", "first": "F"}
CABIN_NAMES = {v: k for k, v in CABINS.items()}
CABIN_LABELS = {"Y": "Economy", "W": "Premium", "J": "Business", "F": "First"}

REGIONS = ["North America", "South America", "Europe", "Asia", "Oceania", "Africa"]

# Major airports per seats.aero region, used when a travel window names a region.
REGION_AIRPORTS = {
    "North America": "JFK,EWR,LGA,BOS,IAD,DCA,ORD,ATL,MIA,FLL,MCO,DFW,IAH,DEN,PHX,LAS,LAX,SFO,SEA,SAN,"
                     "PDX,MSP,DTW,PHL,CLT,YVR,YYZ,YUL,YYC,HNL,OGG,KOA,LIH,CUN,SJD,MEX,PVR,SJU,PUJ,NAS,"
                     "MBJ,AUA,SJO,LIR,PTY,GCM",
    "South America": "GRU,GIG,EZE,AEP,SCL,LIM,BOG,MDE,UIO,GYE,CTG,MVD,ASU,CNF,REC,SSA,BSB,FOR,CUZ",
    "Europe": "LHR,LGW,CDG,ORY,AMS,FRA,MUC,ZRH,GVA,VIE,FCO,MXP,MAD,BCN,LIS,OPO,DUB,CPH,ARN,OSL,"
              "HEL,BRU,ATH,IST,PRG,BUD,WAW,EDI,MAN,NCE,BER,KEF,VCE,NAP,MLA",
    "Asia": "NRT,HND,KIX,ICN,TPE,HKG,PVG,PEK,PKX,CAN,MNL,SIN,BKK,HKT,KUL,CGK,DPS,SGN,HAN,DEL,"
            "BOM,BLR,DXB,DOH,AUH,TLV,AMM,RUH,JED,CMB,MLE,KTM,FUK,CTS,OKA,CEB,DAD",
    "Oceania": "SYD,MEL,BNE,AKL,PER,ADL,CHC,NAN,PPT,ZQN,CNS,OOL,WLG",
    "Africa": "JNB,CPT,NBO,ADD,CAI,CMN,RAK,LOS,ACC,DKR,SEZ,MRU,ZNZ,DAR,KGL,WDH,VFA",
}
AIRPORT_REGION = {a: r for r, lst in REGION_AIRPORTS.items() for a in lst.split(",")}

# City / metro codes accepted anywhere an airport list is accepted.
METROS = {
    "NYC": "JFK,EWR,LGA", "WAS": "IAD,DCA,BWI", "CHI": "ORD,MDW", "BAY": "SFO,OAK,SJC",
    "LAX*": "LAX,BUR,LGB,SNA,ONT", "HOU": "IAH,HOU", "DFW*": "DFW,DAL", "MIA*": "MIA,FLL",
    "LON": "LHR,LGW,LCY,STN", "PAR": "CDG,ORY", "MIL": "MXP,LIN", "TYO": "NRT,HND",
    "OSA": "KIX,ITM", "SEL": "ICN,GMP", "BJS": "PEK,PKX", "SHA": "PVG,SHA", "BUE": "EZE,AEP",
    "RIO": "GIG,SDU", "SAO": "GRU,CGH,VCP", "YTO": "YYZ,YTZ", "YMQ": "YUL", "STO": "ARN",
    "ROM": "FCO,CIA", "MOW": "SVO,DME",
}

# Accounts shown on the Points page. Airline account ids equal seats.aero source codes.
BANK_ACCOUNTS = [
    ("amex_mr", "Amex Membership Rewards"),
    ("chase_ur", "Chase Ultimate Rewards"),
    ("citi_ty", "Citi ThankYou Points"),
    ("capone", "Capital One Miles"),
    ("bilt", "Bilt Rewards"),
    ("hsbc_us", "HSBC US Rewards"),
    ("wells_fargo", "Wells Fargo Rewards"),
]
AIRLINE_ACCOUNTS = [
    ("united", "United MileagePlus"),
    ("american", "American AAdvantage"),
    ("delta", "Delta SkyMiles"),
    ("alaska", "Atmos Rewards (Alaska/Hawaiian)"),
    ("jetblue", "JetBlue TrueBlue"),
    ("aeroplan", "Air Canada Aeroplan"),
    ("flyingblue", "Air France/KLM Flying Blue"),
    ("qatar", "Avios (Qatar / British Airways / Iberia / Aer Lingus)"),
    ("virginatlantic", "Virgin Atlantic Flying Club"),
    ("singapore", "Singapore KrisFlyer"),
    ("emirates", "Emirates Skywards"),
    ("etihad", "Etihad Guest"),
    ("turkish", "Turkish Miles&Smiles"),
    ("lifemiles", "Avianca LifeMiles"),
    ("aeromexico", "Aeromexico Rewards"),
    ("qantas", "Qantas Frequent Flyer"),
    ("velocity", "Virgin Australia Velocity"),
    ("lufthansa", "Lufthansa Miles & More"),
    ("eurobonus", "SAS EuroBonus"),
    ("finnair", "Finnair Plus (Avios)"),
    ("ethiopian", "Ethiopian ShebaMiles"),
    ("saudia", "Saudia AlFursan"),
    ("connectmiles", "Copa ConnectMiles"),
    ("smiles", "GOL Smiles"),
    ("azul", "Azul Fidelidade"),
]
# Programs worth tracking balances for but not searchable on seats.aero.
OTHER_ACCOUNTS = [
    ("cathay", "Cathay Asia Miles"),
    ("ana", "ANA Mileage Club"),
    ("jal", "JAL Mileage Bank"),
    ("eva", "EVA Infinity MileageLands"),
    ("southwest", "Southwest Rapid Rewards"),
]
SHORT_NAMES = {
    "amex_mr": "Amex MR", "chase_ur": "Chase UR", "citi_ty": "Citi TY", "capone": "Capital One", "bilt": "Bilt",
    "hsbc_us": "HSBC", "wells_fargo": "Wells Fargo", "united": "United", "american": "American", "delta": "Delta",
    "alaska": "Atmos/Alaska", "jetblue": "JetBlue", "aeroplan": "Aeroplan", "flyingblue": "Flying Blue",
    "qatar": "Avios (Qatar)", "virginatlantic": "Virgin Atlantic", "singapore": "KrisFlyer", "emirates": "Emirates",
    "etihad": "Etihad", "turkish": "Turkish", "lifemiles": "LifeMiles", "aeromexico": "Aeromexico", "qantas": "Qantas",
    "velocity": "Velocity", "lufthansa": "Miles & More", "eurobonus": "EuroBonus", "finnair": "Finnair (Avios)",
    "ethiopian": "ShebaMiles", "saudia": "AlFursan", "connectmiles": "ConnectMiles", "smiles": "Smiles", "azul": "Azul",
    "cathay": "Asia Miles", "ana": "ANA", "jal": "JAL", "eva": "EVA", "southwest": "Southwest",
}
ACCOUNT_NAMES = dict(BANK_ACCOUNTS + AIRLINE_ACCOUNTS + OTHER_ACCOUNTS)
SOURCE_NAMES = dict(AIRLINE_ACCOUNTS)

# Deal thresholds: one-way miles per person at or below which an award is "good".
# Bands are route great-circle distance in miles (upper bounds; last is open-ended).
DISTANCE_BANDS = [1500, 3500, 6000, 8000, 99999]
DEFAULT_THRESHOLDS = {
    "Y": [10000, 20000, 30000, 40000, 45000],
    "W": [15000, 30000, 45000, 55000, 65000],
    "J": [20000, 40000, 60000, 75000, 85000],
    "F": [30000, 55000, 80000, 100000, 110000],
}

DEFAULT_SETTINGS = {
    "home_airports": "",           # e.g. "JFK,EWR,LGA"
    "ondemand_reserve": 100,       # calls held back for Claude / "run now"; released as the day ends
    "cpp": 1.5,                    # cents per point, used to penalise high taxes/surcharges
    "tax_allowance_usd": 100,      # taxes up to this amount are not penalised
    "thresholds": DEFAULT_THRESHOLDS,
    "mac_notify": True,
    "ntfy_enabled": False,
    "ntfy_topic": "",
    "ntfy_server": "https://ntfy.sh",
    "great_score": 1.25,           # score at/above which a deal is labelled "great"
    "paused": False,
    "engine": "local",             # "cloud": hourly Claude routine polls and judges; this Mac only syncs
    "judge_min_score": 0.75,       # loose price filter before Claude judges a window deal
    "judge_batch": 40,             # max candidates Claude judges per run (rest wait for the next run)
    "judge_notes": "",             # what Claude should know about your preferences
}
