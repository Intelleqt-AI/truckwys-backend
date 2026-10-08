"""Deterministic pre-parser for spoken/typed quote requests (English, Afrikaans
and the mixed "Afrikaans-English" SA transporters actually speak).

Works with no LLM at all, so the basics (route, weight, cargo, truck, dates,
trip shape, cross-border) still fill when the model is down, slow or not
configured — and when it fully explains a message, the paid LLM call can be
skipped entirely (see quote_nl.understand).

Contract: never invent. A field is only set when a rule matched it; anything
the parser saw but could not resolve goes into `not_understood`, and every
set field carries a confidence in [0, 1]. Customer matching is local only —
the customer list never leaves this process.

Everything runs on one normalised string (lowercase, accents stripped,
number words converted to digits) so every rule can record the character
span it consumed; the uncovered remainder (`residue`) is what tells the
caller whether an LLM could add anything.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ── Limits (sanity) ──────────────────────────────────────────────────────────
MAX_WEIGHT_KG = 60_000.0      # heaviest legal SA combination payload is ~37 t; 60 t leaves room for abnormal
MIN_WEIGHT_KG = 1.0
MAX_DRIVER_NIGHTS = 30
FUEL_PRICE_RANGE = (5.0, 100.0)  # R/L, same bounds as the fuel-price settings
MAX_DATE_AHEAD_DAYS = 366

# ── Gazetteer ────────────────────────────────────────────────────────────────
# canonical name (what the geocoder gets), ISO country, aliases (normalised
# form is derived). Includes Afrikaans spellings, isiZulu/Sesotho/Setswana
# official names, trucker shorthand and common STT spellings.
_PLACES: List[Tuple[str, str, Sequence[str]]] = [
    ("Johannesburg", "ZA", ["johannesburg", "joburg", "jo burg", "jozi", "jhb", "joeys", "egoli", "e goli",
                            "johannesberg", "johannes burg", "jo'burg", "j h b"]),
    ("Cape Town", "ZA", ["cape town", "capetown", "kaapstad", "kaap stad", "cpt", "c p t", "ikapa", "the cape", "die kaap"]),
    ("Durban", "ZA", ["durban", "dbn", "d b n", "ethekwini", "e thekwini", "thekwini", "durbs", "theku"]),
    ("Pretoria", "ZA", ["pretoria", "pta", "p t a", "tshwane", "pitori", "pitoli"]),
    ("Bloemfontein", "ZA", ["bloemfontein", "bloem", "bfn", "mangaung", "bloemfontain", "bloemfontien"]),
    ("Gqeberha", "ZA", ["gqeberha", "port elizabeth", "pe", "p e", "the bay", "nelson mandela bay", "ibhayi",
                        "port elisabeth", "kebera", "gqebera"]),
    ("East London", "ZA", ["east london", "oos londen", "oos-londen", "el", "buffalo city", "emonti", "oos londe"]),
    ("Polokwane", "ZA", ["polokwane", "pietersburg", "pietersberg"]),
    ("Mbombela", "ZA", ["mbombela", "nelspruit", "nelsspruit"]),
    ("Richards Bay", "ZA", ["richards bay", "richardsbaai", "richards baai", "richardsbay", "rbay", "r bay",
                            "umhlathuze", "richard's bay"]),
    ("Vereeniging", "ZA", ["vereeniging", "vereniging", "vaal"]),
    ("Vanderbijlpark", "ZA", ["vanderbijlpark", "vanderbijl park", "vdbp"]),
    ("Sasolburg", "ZA", ["sasolburg"]),
    ("Rustenburg", "ZA", ["rustenburg", "rusternburg"]),
    ("Upington", "ZA", ["upington"]),
    ("Kimberley", "ZA", ["kimberley", "kimberly"]),
    ("Pietermaritzburg", "ZA", ["pietermaritzburg", "maritzburg", "pmb", "msunduzi", "umgungundlovu",
                                "pietermaritsburg"]),
    ("George", "ZA", ["george"]),
    ("Mossel Bay", "ZA", ["mossel bay", "mosselbaai", "mossel baai"]),
    ("Knysna", "ZA", ["knysna"]),
    ("Oudtshoorn", "ZA", ["oudtshoorn"]),
    ("Worcester", "ZA", ["worcester"]),
    ("Paarl", "ZA", ["paarl", "die paarl"]),
    ("Stellenbosch", "ZA", ["stellenbosch"]),
    ("Saldanha", "ZA", ["saldanha", "saldanha bay", "saldanhabaai"]),
    ("Atlantis", "ZA", ["atlantis"]),
    ("Bellville", "ZA", ["bellville"]),
    ("Beaufort West", "ZA", ["beaufort west", "beaufort wes"]),
    ("Springbok", "ZA", ["springbok"]),
    ("Welkom", "ZA", ["welkom"]),
    ("Kroonstad", "ZA", ["kroonstad"]),
    ("Bethlehem", "ZA", ["bethlehem"]),
    ("Harrismith", "ZA", ["harrismith"]),
    ("Klerksdorp", "ZA", ["klerksdorp", "matlosana"]),
    ("Potchefstroom", "ZA", ["potchefstroom", "potch", "tlokwe"]),
    ("Mahikeng", "ZA", ["mahikeng", "mafikeng", "mafeking"]),
    ("Witbank", "ZA", ["witbank", "emalahleni", "e malahleni"]),
    ("Middelburg", "ZA", ["middelburg", "middleburg"]),
    ("Secunda", "ZA", ["secunda"]),
    ("Ermelo", "ZA", ["ermelo"]),
    ("Standerton", "ZA", ["standerton"]),
    ("Newcastle", "ZA", ["newcastle", "nuwe kasteel"]),
    ("Ladysmith", "ZA", ["ladysmith"]),
    ("Vryheid", "ZA", ["vryheid"]),
    ("Empangeni", "ZA", ["empangeni"]),
    ("Port Shepstone", "ZA", ["port shepstone"]),
    ("Ballito", "ZA", ["ballito"]),
    ("Tzaneen", "ZA", ["tzaneen"]),
    ("Phalaborwa", "ZA", ["phalaborwa"]),
    ("Lephalale", "ZA", ["lephalale", "ellisras"]),
    ("Mokopane", "ZA", ["mokopane", "potgietersrus"]),
    ("Bela-Bela", "ZA", ["bela bela", "warmbad", "warmbaths"]),
    ("Musina", "ZA", ["musina", "messina"]),
    ("Komatipoort", "ZA", ["komatipoort"]),
    ("Mthatha", "ZA", ["mthatha", "umtata"]),
    ("Komani", "ZA", ["komani", "queenstown"]),
    ("Makhanda", "ZA", ["makhanda", "grahamstown", "grahamstad"]),
    ("Kariega", "ZA", ["kariega", "uitenhage"]),
    ("Coega", "ZA", ["coega", "ngqura"]),
    ("Cradock", "ZA", ["cradock", "nxuba"]),
    ("Graaff-Reinet", "ZA", ["graaff reinet", "graaf reinet"]),
    ("Kuruman", "ZA", ["kuruman"]),
    ("Brits", "ZA", ["brits"]),
    ("Centurion", "ZA", ["centurion"]),
    ("Midrand", "ZA", ["midrand"]),
    ("Germiston", "ZA", ["germiston"]),
    ("Kempton Park", "ZA", ["kempton park", "kemptonpark"]),
    ("Isando", "ZA", ["isando"]),
    ("City Deep", "ZA", ["city deep"]),
    ("Benoni", "ZA", ["benoni"]),
    ("Boksburg", "ZA", ["boksburg"]),
    ("Alberton", "ZA", ["alberton"]),
    ("Springs", "ZA", ["springs"]),
    ("Krugersdorp", "ZA", ["krugersdorp", "mogale city"]),
    ("Hermanus", "ZA", ["hermanus"]),
    ("Walvis Bay", "NA", ["walvis bay", "walvisbaai", "walvis baai"]),
    ("Windhoek", "NA", ["windhoek", "windhuk"]),
    ("Keetmanshoop", "NA", ["keetmanshoop"]),
    ("Gaborone", "BW", ["gaborone", "gabs", "gaberone"]),
    ("Francistown", "BW", ["francistown"]),
    ("Maseru", "LS", ["maseru"]),
    ("Mbabane", "SZ", ["mbabane"]),
    ("Manzini", "SZ", ["manzini"]),
    ("Maputo", "MZ", ["maputo", "lourenco marques"]),
    ("Beira", "MZ", ["beira"]),
    ("Harare", "ZW", ["harare"]),
    ("Beitbridge", "ZW", ["beitbridge", "beit bridge"]),
    ("Bulawayo", "ZW", ["bulawayo"]),
    ("Lusaka", "ZM", ["lusaka"]),
    ("Ndola", "ZM", ["ndola"]),
    ("Lubumbashi", "CD", ["lubumbashi"]),
    ("Kolwezi", "CD", ["kolwezi"]),
    ("Lilongwe", "MW", ["lilongwe"]),
    ("Blantyre", "MW", ["blantyre"]),
]

# Countries spoken as a destination ("na Namibië toe", "to Zim").
_COUNTRIES: List[Tuple[str, str, Sequence[str]]] = [
    ("Namibia", "NA", ["namibia", "namibie", "suidwes", "south west"]),
    ("Botswana", "BW", ["botswana"]),
    ("Zimbabwe", "ZW", ["zimbabwe", "zim", "zimbabwe"]),
    ("Mozambique", "MZ", ["mozambique", "mosambiek", "moz"]),
    ("Zambia", "ZM", ["zambia", "zambie"]),
    ("Lesotho", "LS", ["lesotho"]),
    ("Eswatini", "SZ", ["eswatini", "swaziland", "swaziland"]),
    ("Malawi", "MW", ["malawi"]),
    ("DRC", "CD", ["drc", "congo", "kongo"]),
    ("Angola", "AO", ["angola"]),
    ("Tanzania", "TZ", ["tanzania", "tanzanie"]),
]

# Border posts. Canonical names are exactly cross_border.BORDER_POSTS' names
# (what route/border costing uses), so an extracted border_post can be looked
# up directly; aliases are each side's own name plus spoken/Afrikaans forms.
_BORDER_EXTRA_ALIASES = {
    "Beitbridge": ["beit bridge", "beitbrug", "bietbridge", "bb border"],
    "Groblersbrug / Martin's Drift": ["grobler's bridge", "groblers bridge", "grobblersbrug", "martins drift"],
    "Kopfontein / Tlokweng": ["kop fontein", "tlokweng gate"],
    "Skilpadshek / Pioneer Gate": ["skilpadshek border", "pioneer"],
    "Lebombo / Ressano Garcia": ["ressano", "komatipoort border", "lebombo border"],
    "Kosi Bay / Ponta do Ouro": ["kosibaai", "kosi baai"],
    "Trans-Kalahari: Mamuno / Buitepos": ["trans kalahari", "mamuno", "buitepos"],
    "Nakop / Ariamsvlei": ["nakop border", "ariamsvlei"],
    "Vioolsdrif / Noordoewer": ["vioolsdrift", "violsdrif", "noordoewer"],
    "Maseru Bridge": ["maserubrug", "maseru brug", "maseru border"],
    "Ficksburg Bridge / Maputsoe": ["ficksburg border", "ficksburgbrug"],
    "Oshoek / Ngwenya": ["oshoek border", "os hoek"],
    "Golela / Lavumisa": ["golela border"],
}
# Single-word aliases that are also ordinary words or towns: never border posts alone.
_BORDER_SKIP_ALIASES = {"bray", "pioneer", "sendelingsdrif", "alexander bay", "ficksburg"}


def _border_posts() -> List[Tuple[str, Sequence[str]]]:
    from core.services.cross_border import BORDER_POSTS
    rows = []
    for name, _lat, _lng in BORDER_POSTS:
        base = name.split(":", 1)[-1]
        aliases = [part.strip() for part in base.split("/") if part.strip()]
        aliases += [name] + _BORDER_EXTRA_ALIASES.get(name, [])
        aliases = [a for a in aliases if norm_phrase(a) not in _BORDER_SKIP_ALIASES]
        rows.append((name, aliases))
    # Only SA-neighbour posts (cross_border.BORDER_POSTS): border costing knows
    # nothing else, so e.g. Kasumbalesa/Chirundu are never filled as a post.
    return rows


# Aliases that are also ordinary words / names: only a place when a route
# marker sits right before them ("to George", "na die Kaap").
_AMBIGUOUS_ALIASES = {"george", "el", "pe", "p e", "the cape", "die kaap", "vaal", "the bay", "springs",
                      "welkom", "brits", "moz", "zim", "gabs", "potch", "bloem", "atlantis", "theku",
                      "durbs", "joeys", "springbok", "bethlehem", "worcester", "newcastle", "middelburg"}

# ── Cargo lexicon (Afrikaans/English/STT spellings → canonical English) ──────
_CARGO: List[Tuple[str, Sequence[str]]] = [
    ("steel coils", ["staalrolle", "staal rolle", "staalrol", "steel coils", "steel coil", "coils of steel",
                     "rolle staal", "coils", "staalspoele", "steal coils", "steal coil"]),
    ("steel pipes", ["staalpype", "steel pipes", "steel pipe"]),
    ("steel beams", ["staalbalke", "steel beams", "i beams", "ibeams"]),
    ("steel", ["staal", "steel", "rebar", "wapeningstaal"]),
    ("frozen chicken", ["bevrore hoender", "frozen chicken"]),
    ("frozen goods", ["bevrore goedere", "frozen goods", "frozen food", "bevrore kos", "frozen"]),
    ("chilled goods", ["verkoelde goedere", "chilled goods", "chilled"]),
    ("cement", ["sement", "cement"]),
    ("maize meal", ["mielie meel", "mieliemeel", "mealie meal", "maize meal", "mielie pap", "mieliepap"]),
    ("maize", ["mielies", "mielie", "mealies", "maize"]),
    ("wheat", ["koring", "wheat"]),
    ("sunflower seed", ["sonneblomsaad", "sonneblom", "sunflower seed", "sunflowers", "sunflower"]),
    ("soya beans", ["sojabone", "soja", "soya beans", "soybeans", "soya"]),
    ("sugar", ["suiker", "sugar"]),
    ("flour", ["meel", "flour"]),
    ("rice", ["rys", "rice"]),
    ("fertiliser", ["kunsmis", "fertiliser", "fertilizer", "bemesting"]),
    ("coal", ["steenkool", "kole", "coal"]),
    ("sand", ["sand"]),
    ("aggregate", ["klippe", "klip", "gruis", "gravel", "aggregate", "crushed stone"]),
    ("bricks", ["bakstene", "stene", "bricks", "brick"]),
    ("timber", ["hout", "timber", "planke", "wood", "lumber"]),
    ("poles", ["pale", "paal", "poles", "gum poles"]),
    ("citrus", ["sitrus", "citrus", "lemoene", "oranges", "naartjies"]),
    ("apples", ["appels", "apples"]),
    ("grapes", ["druiwe", "grapes"]),
    ("fruit", ["vrugte", "fruit"]),
    ("potatoes", ["aartappels", "potatoes", "spuds"]),
    ("onions", ["uie", "onions"]),
    ("vegetables", ["groente", "vegetables", "veggies"]),
    ("meat", ["vleis", "meat", "beef", "beesvleis"]),
    ("chicken", ["hoender", "chicken", "poultry", "pluimvee"]),
    ("fish", ["vis", "fish"]),
    ("milk", ["melk", "milk"]),
    ("dairy", ["suiwel", "dairy"]),
    ("beer", ["bier", "beer"]),
    ("wine", ["wyn", "wine"]),
    ("beverages", ["koeldrank", "cooldrinks", "cool drinks", "drinks", "beverages", "drank", "cold drinks",
                   "soft drinks"]),
    ("bottled water", ["bottelwater", "bottled water"]),
    ("diesel", ["diesel", "diesel fuel"]),
    ("petrol", ["petrol"]),
    ("fuel", ["brandstof", "fuel", "paraffien", "paraffin"]),
    ("lubricants", ["olie", "oil", "lubricants", "smeermiddels"]),
    ("chemicals", ["chemikaliee", "chemikalie", "chemicals", "chemical"]),
    ("LPG", ["lpg", "gas bottles", "gasbottels"]),
    ("machinery", ["masjinerie", "masjiene", "machinery", "machines", "plant equipment"]),
    ("equipment", ["toerusting", "equipment"]),
    ("vehicles", ["motors", "karre", "cars", "voertuie", "vehicles"]),
    ("tractors", ["trekkers", "tractors"]),
    ("containers", ["houers", "containers", "container"]),
    ("furniture", ["meubels", "meubles", "furniture"]),
    ("electronics", ["elektronika", "electronics"]),
    ("clothing", ["klere", "clothing", "clothes", "textiles", "tekstiel"]),
    ("paper rolls", ["papierrolle", "paper rolls", "paper reels"]),
    ("paper", ["papier", "paper"]),
    ("cardboard", ["karton", "cardboard", "boxes"]),
    ("plastics", ["plastiek", "plastic", "plastics"]),
    ("glass", ["glas", "glass"]),
    ("chrome ore", ["chroom", "chrome ore", "chrome"]),
    ("manganese ore", ["mangaan", "manganese ore", "manganese"]),
    ("iron ore", ["ystererts", "iron ore"]),
    ("copper cathodes", ["koperkatodes", "copper cathodes", "copper cathode"]),
    ("copper", ["koper", "copper"]),
    ("ore", ["erts", "ore"]),
    ("scrap metal", ["skroot", "skrootmetaal", "scrap metal", "scrap"]),
    ("livestock", ["vee", "beeste", "cattle", "livestock", "skape", "sheep"]),
    ("chicken feed", ["hoendervoer", "chicken feed", "poultry feed"]),
    ("animal feed", ["veevoer", "voer", "animal feed", "feed", "lucerne", "lusern"]),
    ("hay bales", ["hooi", "hay bales", "hay", "baale"]),
    ("groceries", ["kruideniersware", "groceries", "food", "kos"]),
    ("tyres", ["bande", "tyres", "tires"]),
    ("roof sheeting", ["dakplate", "roof sheeting", "roofing sheets", "zinc sheets", "sinkplate"]),
    ("pipes", ["pype", "pipes", "pvc pipes"]),
    ("cables", ["kabels", "cables", "cable drums"]),
    ("pharmaceuticals", ["medisyne", "pharmaceuticals", "medicine"]),
    ("parcels", ["pakkies", "parcels"]),
    ("general cargo", ["algemene vrag", "general cargo", "general freight", "general goods", "algemene goedere"]),
    ("pallets", ["palette", "pallets", "pallet", "palet"]),
]

# ── Vehicle hints → (canonical label, alias phrases to match a fleet name) ──
_VEHICLES: List[Tuple[str, str, Sequence[str], Sequence[str]]] = [
    # key, English label, spoken aliases, fleet-matching phrases
    ("interlink", "Superlink", ["superlink", "super link", "superlinks", "interlink", "inter link", "b train",
                                "btrain", "b trein"], ["superlink", "interlink", "b-train"]),
    ("tautliner", "Tautliner", ["tautliner", "taut liner", "tautliners", "gordynkant", "gordyn kant",
                                "gordynwa", "curtainsider", "curtain sider", "curtain side", "tauty", "taut"],
     ["tautliner", "curtainsider", "taut"]),
    ("reefer", "Refrigerated truck", ["koelwa", "koel wa", "koeltrok", "koel trok", "reefer", "refrigerated",
                                      "fridge truck", "yskas trok", "yskastrok", "verkoelde trok", "fridge",
                                      "refrigerator truck", "koelhouer"], ["reefer", "refrigerated", "fridge"]),
    ("tipper", "Tipper", ["tipper", "tip truck", "wipbak", "wip bak", "side tipper", "sytipper", "kipper",
                          "tipwa", "dump truck"], ["tipper", "tip"]),
    ("flatbed", "Flatbed", ["flatbed", "flat bed", "flat deck", "flatdeck", "platbak", "plat bak", "platdek",
                            "flattie"], ["flatbed", "flat deck"]),
    ("dropside", "Dropside", ["dropside", "drop side", "valkant"], ["dropside"]),
    ("tanker", "Tanker", ["tanker", "tenkwa", "tenk wa", "tenkwaens", "tank truck"], ["tanker", "tank"]),
    ("lowbed", "Lowbed", ["lowbed", "low bed", "laebed", "lae bed", "lowboy", "low loader"], ["lowbed", "low bed"]),
    ("car_carrier", "Car carrier", ["car carrier", "car transporter", "motordraer", "karre draer"],
     ["car carrier", "car transporter"]),
    ("ldv", "LDV / bakkie", ["bakkie", "ldv", "light delivery"], ["ldv", "light delivery", "bakkie"]),
    ("semi", "Horse and trailer", ["horse and trailer", "perd en sleepwa", "semi trailer", "semi",
                                   "sleepwa", "trailer"], ["semi", "horse"]),
    ("rigid", "Rigid truck", ["rigid", "rigid truck", "stywe trok"], ["rigid"]),
]

_VEHICLE_NOUNS = ("truck|trucks|trok|trokke|lorry|vragmotor|vragmotors|rigid|bakkie|superlink|interlink|"
                  "tautliner|tipper|flatbed|reefer|koelwa|wipbak|platbak|tanker|trailer|sleepwa|horse|perd|ldv|"
                  "vehicle|voertuig|gordynkant|lowbed|laebed")

# ── Number words ─────────────────────────────────────────────────────────────
_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "nul": 0, "een": 1, "twee": 2, "drie": 3, "vier": 4, "vyf": 5, "ses": 6, "sewe": 7, "agt": 8, "nege": 9,
    "tien": 10, "elf": 11, "twaalf": 12, "dertien": 13, "veertien": 14, "vyftien": 15, "sestien": 16,
    "sewentien": 17, "agttien": 18, "agtien": 18, "negentien": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90,
    "twintig": 20, "dertig": 30, "veertig": 40, "vyftig": 50, "sestig": 60, "sewentig": 70, "tagtig": 80,
    "negentig": 90,
}
_SCALES = {"hundred": 100, "honderd": 100, "thousand": 1000, "duisend": 1000}
_AF_UNIT_RE = "een|twee|drie|vier|vyf|ses|sewe|agt|ag|nege"
_AF_TENS_RE = "twintig|dertig|veertig|vyftig|sestig|sewentig|tagtig|negentig"

# ── Dates ────────────────────────────────────────────────────────────────────
_MONTHS = {
    "january": 1, "jan": 1, "januarie": 1, "february": 2, "feb": 2, "februarie": 2, "march": 3, "mar": 3,
    "maart": 3, "april": 4, "apr": 4, "may": 5, "mei": 5, "june": 6, "jun": 6, "junie": 6, "july": 7,
    "jul": 7, "julie": 7, "august": 8, "aug": 8, "augustus": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "oktober": 10, "okt": 10, "november": 11, "nov": 11, "december": 12,
    "dec": 12, "desember": 12, "des": 12,
}
_MONTH_SHORT = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
_WEEKDAYS = {
    "monday": 0, "mon": 0, "maandag": 0, "tuesday": 1, "tue": 1, "tues": 1, "dinsdag": 1,
    "wednesday": 2, "wed": 2, "woensdag": 2, "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "donderdag": 3, "friday": 4, "fri": 4, "vrydag": 4, "vrijdag": 4, "vridag": 4, "frydag": 4,
    "saturday": 5, "saterdag": 5, "sunday": 6, "sondag": 6,
}

# ── Language hint lexicons ───────────────────────────────────────────────────
_AF_WORDS = {
    "van", "vanaf", "na", "toe", "die", "n", "ek", "het", "nodig", "vir", "met", "trok", "trokke", "vrag",
    "leeg", "terug", "nie", "ons", "moet", "asseblief", "kan", "jy", "wil", "laai", "aflaai", "oplaai", "dae",
    "volgende", "teen", "sleepwa", "koelwa", "oormore", "vandag", "heen", "retoervrag", "terugvrag", "staal",
    "staalrolle", "kwotasie", "prys", "hoeveel", "kos", "gaan", "sal", "uit", "oor", "tot", "wat", "hierdie",
    "dit", "daar", "hier", "ook", "maar", "gewig", "goedere", "maandag", "dinsdag", "woensdag", "donderdag",
    "vrydag", "saterdag", "sondag", "kaapstad", "rigting", "enkel", "gordynkant", "wipbak", "platbak", "nag",
    "nagte", "brandstof", "klient", "grens", "oorgrens", "mielies", "sement", "hout", "vrugte", "palette",
    "aflewer", "afgelewer", "is", "en", "goeiemore", "goeiemiddag", "dankie", "tagtig",
    "twintig", "dertig", "veertig", "agt", "twee", "drie", "vier", "vyf", "ses", "sewe", "nege", "tien",
    "oggend", "middag", "aand", "soek", "vat", "wees", "bevrore", "steenkool", "suiker", "druiwe",
}
_EN_WORDS = {
    "from", "to", "the", "and", "of", "for", "with", "tomorrow", "please", "need", "quote", "empty", "back",
    "truck", "tons", "tonnes", "steel", "on", "at", "a", "we", "i", "can", "you", "round", "trip", "one",
    "way", "load", "return", "deliver", "delivery", "pickup", "pick", "up", "collect", "next", "friday",
    "monday", "client", "customer", "border", "cross", "is", "and", "twenty", "thirty", "eight",
}
# Words both languages share with identical spelling don't count for either.
_SHARED = {"is", "en", "and", "in", "op", "by", "ton", "kilo", "superlink", "tautliner", "trailer"}

# Filler: words that carry no quote information in either language. Used to
# decide whether anything in the message is still unexplained.
_FILLER = set("""
i we ek ons me my our you jy u julle they hulle he she hy sy it its dit this that hierdie daardie die the a an n
please asseblief pls plz asb need needs nodig want wil wants would sal will can kan could kon may mag must moet
should behoort get kry got give gee het have has had am are was were be wees is been
quote quotation kwotasie kwota price prys rate tarief cost kos costs how much hoeveel what wat
for vir of van met with and en on op at by in into uit to na naar tot from vanaf via oor deur
load loads vrag vragte trip rit job werk truck trucks trok trokke lorry vragmotor vehicle voertuig
going gaan go move moving transport vervoer ship send stuur take neem carry dra deliver delivered
there daar here hier also ook just net only slegs some about approx approximately around ongeveer sowat
so ok okay hi hello hallo hey howzit goeie goeiemore goeiemiddag goeienaand middag dag morning afternoon
evening thanks thank dankie baie then dan um uh eh ja yes like guys man boet bru ou please hey mate
total totaal weight gewig weighs weeg cargo goods goedere stuff full vol worth need it them
from-to a-to-b one two some lot lots ton tons tonne tonnes kg kilo kilos t
trip-type when wanneer date datum day dag days dae be by please thx cheers lekker asseblief
the-client also nog still more nogal sommer gou quick quickly vinnig asap urgent dringend
pick picked picking pickup up collect collection collected deliver delivery delivering drop off
dropoff offload aflaai oplaai laai loaded loading aflewer aflewering afgelewer
do doen does want please per each elke all alles
soek look looking af haal kom come wee sien see plek place
something anything somewhere anywhere iets iewers erens whatever
through thru ve ll re d teen vat stop port hawe harbour depot
""".split())

_DELIVERY_MARKERS = (r"to|2|na|naar|tot|into|till|toward|towards|destination|bestemming|"
                     r"deliver(?:ed|ing|y)?(?:\s+(?:to|in|at|on))?|drop(?:\s*off)?(?:\s+(?:at|in))?|"
                     r"off\s*load(?:ing)?(?:\s+(?:at|in))?|offload(?:ing)?(?:\s+(?:at|in))?|"
                     r"afla(?:ai|ai\s+(?:in|by|op))|aflewer(?:ing)?(?:\s+(?:in|by|na|op))?|"
                     r"afgelewer(?:\s+(?:in|by|word))?|going\s+to|gaan\s+na")
_PICKUP_MARKERS = (r"from|frm|fr|van|vanaf|uit|ex|origin|"
                   r"pick(?:ed|ing)?\s*up(?:\s+(?:in|at|from))?|pickup(?:\s+(?:in|at|from))?|"
                   r"collect(?:ion|ed)?(?:\s+(?:in|at|from))?|load(?:ed|ing)?\s+(?:in|at)|"
                   r"oplaai(?:\s+(?:in|by|op))?|laai\s+(?:in|by|op)|optel(?:\s+(?:in|by))?|"
                   r"haal\s+(?:in|by)|afhaal(?:\s+(?:in|by))?")
_STOP_MARKERS = r"via|through|thru|oor|deur|stop(?:ping|s)?\s+(?:in|at|by|over\s+in)|met\s+n\s+stop\s+(?:in|by)"


@dataclass
class PreParse:
    fields: Dict[str, Any] = field(default_factory=dict)
    confidence: Dict[str, float] = field(default_factory=dict)
    not_understood: List[str] = field(default_factory=list)
    # parallel to not_understood: the field each note is about ("weight_missing"
    # = a weight-ish thing was said but no weight could be set), or None
    not_understood_fields: List[Optional[str]] = field(default_factory=list)
    language_hint: Optional[str] = None
    mixed_language: bool = False
    residue: List[str] = field(default_factory=list)
    vehicle_hint: Optional[str] = None          # canonical key, e.g. "interlink"
    vehicle_hint_label: Optional[str] = None    # English label, e.g. "Superlink"
    vehicle_capacity_t: Optional[float] = None  # "8 ton truck" → 8
    customer_span_text: Optional[str] = None    # raw text of the customer mention (for redaction)
    said: Dict[str, str] = field(default_factory=dict)  # field -> the user's own wording, when it differs
    unmatched: Dict[str, Optional[str]] = field(default_factory=lambda: {"customer_name": None,
                                                                          "vehicle_type": None})

    @property
    def sufficient(self) -> bool:
        """True when every content word in the message was explained by a rule
        and nothing was left unresolved — an LLM could add nothing."""
        return bool(self.fields) and not self.residue and not self.not_understood

    def flag(self, text: str, about: Optional[str] = None) -> None:
        if text not in self.not_understood:
            self.not_understood.append(text)
            self.not_understood_fields.append(about)

    def set(self, key: str, value: Any, conf: float) -> None:
        if value in (None, "", []):
            return
        if key in self.fields and self.confidence.get(key, 0) >= conf:
            return
        self.fields[key] = value
        self.confidence[key] = round(min(max(conf, 0.0), 0.99), 2)


# ── Normalisation ────────────────────────────────────────────────────────────
def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm_phrase(s: str) -> str:
    """Normalise an alias/candidate the same way message text is normalised
    (minus number-word conversion)."""
    s = _strip_accents((s or "").lower()).replace("’", "'")
    s = re.sub(r"[-_/]", " ", s)
    s = s.replace("'", " ")
    s = re.sub(r"[^a-z0-9.\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _words_to_numbers(text: str) -> str:
    # Afrikaans unit-en-tens compounds, spaced or joined: "agt-en-twintig",
    # "agtentwintig", "agt en twintig" → 28.
    def _af_compound(m):
        u = 8 if m.group(1) == "ag" else _UNITS[m.group(1)]
        return str(u + _TENS[m.group(2)])
    text = re.sub(rf"\b({_AF_UNIT_RE})\s*en\s*({_AF_TENS_RE})\b", _af_compound, text)
    # Joined scale words: "tweehonderd", "vyfduisend".
    text = re.sub(r"\b(een|twee|drie|vier|vyf|ses|sewe|agt|nege|tien)(honderd|duisend)\b", r"\1 \2", text)
    # "28 duisend", "2 thousand"
    text = re.sub(r"\b(\d+(?:\.\d+)?)\s+(thousand|duisend)\b",
                  lambda m: _fmt_num(float(m.group(1)) * 1000), text)
    text = re.sub(r"\bag(?=\s+(?:ton|tons|t)\b)", "8", text)

    tokens = text.split(" ")
    out: List[str] = []
    i = 0
    n = len(tokens)

    def is_num_word(t):
        return t in _UNITS or t in _TENS or t in _SCALES

    while i < n:
        t = tokens[i]
        # "a hundred", "n duisend"
        if t in ("a", "n") and i + 1 < n and tokens[i + 1] in _SCALES:
            tokens[i] = "one"
            t = "one"
        if not is_num_word(t):
            out.append(t)
            i += 1
            continue
        total, cur, j = 0.0, 0.0, i
        while j < n:
            w = tokens[j]
            if w in _UNITS or w in _TENS:
                cur += _UNITS.get(w, _TENS.get(w, 0))
                j += 1
            elif w in _SCALES:
                scale = _SCALES[w]
                if scale == 100:
                    cur = (cur or 1) * 100
                else:
                    total += (cur or 1) * scale
                    cur = 0
                j += 1
            elif w in ("and", "en") and j + 1 < n and is_num_word(tokens[j + 1]):
                j += 1
            else:
                break
        value = total + cur
        # "... and a half" / "... en n half"
        if j + 2 < n + 0 and tokens[j:j + 3] in (["and", "a", "half"], ["en", "n", "half"], ["en", "n", "halwe"]):
            value += 0.5
            j += 3
        out.append(_fmt_num(value))
        i = j
    return " ".join(out)


_ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
    "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
    "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19, "twentieth": 20,
    "thirtieth": 30,
    "eerste": 1, "tweede": 2, "derde": 3, "vierde": 4, "vyfde": 5, "sesde": 6, "sewende": 7, "agste": 8,
    "negende": 9, "tiende": 10, "elfde": 11, "twaalfde": 12, "dertiende": 13, "veertiende": 14,
    "vyftiende": 15, "sestiende": 16, "sewentiende": 17, "agtiende": 18, "agttiende": 18, "negentiende": 19,
    "twintigste": 20, "dertigste": 30,
}


def _ordinals_to_digits(text: str) -> str:
    """"fourteenth" → "14th", "twenty first" → "21st", "een en twintigste" → "21ste"."""
    def comp_en(m):
        return f"{_TENS[m.group(1)] + _ORDINALS[m.group(2)]}th"
    text = re.sub(r"\b(twenty|thirty)\s+(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth)\b", comp_en, text)
    def comp_af(m):
        u = _UNITS.get(m.group(1), 8)
        return f"{u + {'twintigste': 20, 'dertigste': 30}[m.group(2)]}ste"
    text = re.sub(rf"\b({_AF_UNIT_RE})\s*en\s*(twintigste|dertigste)\b", comp_af, text)
    return re.sub(r"\b(" + "|".join(_ORDINALS) + r")\b", lambda m: f"{_ORDINALS[m.group(1)]}th", text)


def _fmt_num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


def normalise(text: str) -> str:
    """Lowercase, accents stripped, punctuation spaced, SA number formats
    unified, number words → digits. Words whose meaning depends on an accent
    are rewritten to an unambiguous token first ("môre" = tomorrow, but
    "more" is an English word)."""
    s = (text or "").replace("’", "'").replace("‘", "'")
    s = re.sub(r"(?i)\boorm[ôòóõō]re\b", " overmorrow ", s)
    s = re.sub(r"(?i)\bm[ôòóõō]re\b", " tomorrowaf ", s)
    s = s.lower()
    s = _strip_accents(s)
    # Dates with dashes/dots before hyphens become spaces.
    s = re.sub(r"\b(\d{4})[-.](\d{1,2})[-.](\d{1,2})\b", r"\1/\2/\3", s)
    s = re.sub(r"\b(\d{1,2})[-.](\d{1,2})[-.](\d{2,4})\b", r"\1/\2/\3", s)
    # Money: "r23,50" / "r 23.50"
    s = re.sub(r"\br\s?(\d)", r"r \1", s)
    # Decimal comma (SA) vs thousands comma: "28,5" → 28.5; "1,500" → 1500.
    s = re.sub(r"(\d),(\d{3})\b", r"\1\2", s)
    s = re.sub(r"(\d),(\d{1,2})\b", r"\1.\2", s)
    # Space thousands: "28 000" → 28000.
    s = re.sub(r"\b(\d{1,3}) (\d{3})\b(?!\s*/)", r"\1\2", s)
    # "28t", "28kg", "28ton" → "28 t"
    s = re.sub(r"(\d)(t|ton|tons|tonne|tonnes|kg|kgs|kilo|kilos|km)\b", r"\1 \2", s)
    s = re.sub(r"(\d)\s*k\b(?!g)", lambda m: m.group(1) + "000", s)  # "28k kg" rare; "28k" → 28000
    s = s.replace("'", " ")
    s = re.sub(r"[-_]", " ", s)
    # Sentence punctuation is a hard boundary (",") so "32 ton, superlink" is a
    # load weight and a truck, not "a 32 ton superlink".
    s = re.sub(r"(?<!\d)\.|\.(?!\d)", " , ", s)  # keep decimal points only
    s = re.sub(r"[,;:!?()\[\]]", " , ", s)
    s = re.sub(r"[^a-z0-9./,\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return _words_to_numbers(_ordinals_to_digits(s))


def _pre_number_text(text: str) -> str:
    """Normalised but with number words kept — the language hint counts them."""
    s = _strip_accents((text or "").lower().replace("’", "'"))
    s = re.sub(r"[-_']", " ", s)
    return re.sub(r"[^a-z0-9\s]", " ", s)


# ── Compiled alias tables ────────────────────────────────────────────────────
def _alias_table(entries):
    rows = []
    for canonical, extra, aliases in entries:
        for a in aliases:
            na = norm_phrase(a)
            if na:
                rows.append((na, canonical, extra))
    rows.sort(key=lambda r: -len(r[0]))
    return rows


_PLACE_ROWS = _alias_table(_PLACES) + _alias_table(_COUNTRIES)
_COUNTRY_NAMES = {c for c, _, _ in _COUNTRIES}
_BORDER_ROWS = _alias_table([(name, None, aliases) for name, aliases in _border_posts()])
_CARGO_ROWS = _alias_table([(name, None, aliases) for name, aliases in _CARGO])
_VEHICLE_ROWS = _alias_table([(key, label, aliases) for key, label, aliases, _ in _VEHICLES])
_VEHICLE_FLEET_PHRASES = {key: list(phr) for key, _, _, phr in _VEHICLES}
_PLACE_COUNTRY = {c: cc for c, cc, _ in _PLACES}
_PLACE_COUNTRY.update({c: cc for c, cc, _ in _COUNTRIES})
_SINGLE_WORD_PLACE_ALIASES = {na: c for na, c, _ in _PLACE_ROWS if " " not in na and len(na) >= 6}


def _rx(rows) -> re.Pattern:
    return re.compile(r"\b(" + "|".join(re.escape(r[0]) for r in rows) + r")\b")


_PLACE_RX = _rx(_PLACE_ROWS)
_BORDER_RX = _rx(_BORDER_ROWS)
_CARGO_RX = _rx(_CARGO_ROWS)
_VEHICLE_RX = _rx(_VEHICLE_ROWS)
_PLACE_LOOKUP = {r[0]: r[1] for r in _PLACE_ROWS}
_BORDER_LOOKUP = {r[0]: r[1] for r in _BORDER_ROWS}
_CARGO_LOOKUP = {r[0]: r[1] for r in _CARGO_ROWS}
_VEHICLE_LOOKUP = {r[0]: (r[1], r[2]) for r in _VEHICLE_ROWS}

# Every word the rules know about — an unknown-place capture must not swallow these.
_CARGO_WORDS = {w for r in _CARGO_ROWS for w in r[0].split()}
_KNOWN_VOCAB = set(_FILLER) | set(_UNITS) | set(_TENS) | set(_SCALES) | set(_MONTHS) | set(_WEEKDAYS)
for _rows in (_CARGO_ROWS, _VEHICLE_ROWS, _BORDER_ROWS):
    for _r in _rows:
        _KNOWN_VOCAB.update(_r[0].split())
_KNOWN_VOCAB.update({"today", "tomorrow", "tomorrowaf", "overmorrow", "vandag", "next", "volgende", "week",
                     "this", "coming", "komende", "empty", "leeg", "terug", "back", "return", "round", "way",
                     "heen", "retoervrag", "terugvrag", "border", "grens", "cross", "oorgrens", "night", "nights",
                     "nag", "nagte", "diesel", "fuel", "brandstof", "client", "customer", "klient", "valid",
                     "geldig", "until", "days", "dae", "toe", "local", "domestic", "export", "import", "abnormal",
                     "abnormale", "oversize", "permit", "wide"})


def canonical_place(text: str) -> Optional[str]:
    """Canonical gazetteer name for a place string (any alias/spelling), or None."""
    n = norm_phrase(text)
    if not n:
        return None
    if n in _PLACE_LOOKUP:
        return _PLACE_LOOKUP[n]
    m = _PLACE_RX.search(n)
    if m and m.group(1) not in _AMBIGUOUS_ALIASES:
        return _PLACE_LOOKUP[m.group(1)]
    return None


def canonical_border_post(text: str) -> Optional[str]:
    """cross_border.BORDER_POSTS name for a border-post string, or None."""
    m = _BORDER_RX.search(norm_phrase(text))
    return _BORDER_LOOKUP[m.group(1)] if m else None


def geocodable_place(text: str) -> str:
    """The English/official, geocodable name when `text` is exactly a known
    alias ("Kaapstad", "Oos-Londen", "eThekwini", "PE"); anything else
    (a street address, "Cape Town CBD", an unknown town) is returned as given."""
    t = (text or "").strip()
    if not t or re.search(r"\d", t):
        return t
    return _PLACE_LOOKUP.get(norm_phrase(t)) or t


def said_text(raw: str, alias: str) -> Optional[str]:
    """The user's own spelling of a matched alias, as it appears in `raw`."""
    parts = [re.escape(p) for p in alias.split()]
    if not parts:
        return None
    m = re.search(r"(?i)\b" + r"[\W_]*".join(parts) + r"\b", _strip_accents(raw or ""))
    if not m:
        return None
    # same span in the original (accents kept) when lengths line up
    orig = (raw or "")[m.start():m.end()]
    return orig if _strip_accents(orig).lower() == m.group(0).lower() else m.group(0)


def place_country(canonical: Optional[str]) -> Optional[str]:
    return _PLACE_COUNTRY.get(canonical or "")


# ── The parser ───────────────────────────────────────────────────────────────
class _Ctx:
    def __init__(self, text: str, raw: str = ""):
        self.raw = raw
        self.t = text
        self.weight_ends: List[int] = []
        self.spans: List[Tuple[int, int]] = []

    def consume(self, a: int, b: int) -> None:
        self.spans.append((a, b))

    def consumed(self, a: int, b: int) -> bool:
        return any(a < y and b > x for x, y in self.spans)

    def before(self, pos: int, n_chars: int = 40) -> str:
        return self.t[max(0, pos - n_chars):pos]


def _sast_today() -> date:
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime
        return datetime.now(ZoneInfo("Africa/Johannesburg")).date()
    except Exception:  # pragma: no cover
        return date.today()


def preparse(message: str, *, today: Optional[date] = None,
             customers: Optional[List[Dict[str, Any]]] = None,
             vehicle_types: Optional[List[Any]] = None) -> PreParse:
    """Parse one message. `customers` ([{id, name}]) and `vehicle_types`
    ([{name, capacity_t}] or names) are this company's own records, matched
    locally. `today` defaults to the SAST date."""
    today = today or _sast_today()
    out = PreParse()
    raw = message or ""
    text = normalise(raw)
    ctx = _Ctx(text, raw)
    if not text:
        return out

    _language_hint(raw, _pre_number_text(raw), out)
    _weights(ctx, out)
    _trip_shape(ctx, out)
    _border_and_international_keywords(ctx, out)
    _abnormal(ctx, out)
    _vehicles(ctx, out, vehicle_types)
    if out.vehicle_hint == "lowbed" and "abnormal_load" not in out.fields:
        out.set("abnormal_load", True, 0.7)  # a lowbed load is almost always an abnormal
    _dates(ctx, out, today)
    _driver_and_fuel(ctx, out)
    _customers(ctx, out, customers)
    _cargo_after_weight(ctx, out)
    _places(ctx, out)
    _cargo(ctx, out)
    _international_from_places(out)
    if out.fields.get("pickup_date"):
        out.set("trip_date", out.fields["pickup_date"], out.confidence["pickup_date"])
    _distance_mentions(ctx, out)
    _residue(ctx, out)
    return out


def _language_hint(raw: str, text: str, out: PreParse) -> None:
    tokens = text.split()
    af = sum(1 for t in tokens if t in _AF_WORDS and t not in _SHARED)
    af += len(re.findall(r"(?i)\bm[ôòóõō]re\b|\boorm[ôòóõō]re\b|\bkli[eë]nt\b|\bnamibi[eë]\b", raw))
    en = sum(1 for t in tokens if t in _EN_WORDS and t not in _SHARED)
    if af >= 2 and af >= en:
        out.language_hint = "af"
    elif en >= 2 and en > af:
        out.language_hint = "en"
    elif af == 1 and en == 0 and len(tokens) <= 6:
        out.language_hint = "af"
    out.mixed_language = af >= 2 and en >= 2


def _weights(ctx: _Ctx, out: PreParse) -> None:
    rx = re.compile(r"(?<![\d./])(\d+(?:\.\d+)?)\s*(t|ton|tons|tonne|tonnes|tone|tonnage|tonner|"
                    r"kg|kgs|kilo|kilos|kilogram|kilograms|kilogramme|kilogrammes)\b(?:\s+(\w+))?")
    for m in re.finditer(r"\b\d{1,4}\s+(?=(?:pallets?|palette|palet|bags|sakke|units|crates|kratte|boxes|"
                         r"bokse|containers?|houers|loads|vragte|trucks|trokke|cars|motors|karre|drums|vate|"
                         r"bales|baale)\b)", ctx.t):
        ctx.consume(m.start(), m.end())
    found: List[Tuple[float, int, int]] = []
    for m in rx.finditer(ctx.t):
        val = float(m.group(1))
        unit = m.group(2)
        kg = val if unit.startswith("k") else val * 1000
        nxt = m.group(3) or ""
        # "8 ton truck", "'n 34 ton superlink" → a truck size, not the load.
        if re.fullmatch(_VEHICLE_NOUNS, nxt):
            out.vehicle_capacity_t = kg / 1000
            ctx.consume(m.start(), m.end(2))
            continue
        ctx.consume(m.start(), m.end(2))
        ctx.weight_ends.append(m.end(2))
        found.append((kg, m.start(), m.end()))
    if not found:
        # "half a ton" / "n halwe ton"
        m = re.search(r"\b(?:half a|n halwe|halwe|half)\s+(?:ton|tonne)\b", ctx.t)
        if m:
            ctx.consume(m.start(), m.end())
            found.append((500.0, m.start(), m.end()))
    if not found:
        return
    kgs = {round(f[0], 1) for f in found}
    kg = found[0][0]
    if len(kgs) > 1:
        out.flag("more than one weight mentioned \u2014 which is the load?", "weight_invalid")
        return
    if not (MIN_WEIGHT_KG <= kg <= MAX_WEIGHT_KG):
        out.flag(f"weight {_fmt_num(kg / 1000)} t looks wrong", "weight_invalid")
        return
    out.set("weight", kg, 0.95)


def _trip_shape(ctx: _Ctx, out: PreParse) -> None:
    t = ctx.t
    patterns_round = [
        r"\bround\s*trips?\b", r"\breturn\s+trips?\b", r"\bthere\s+and\s+back\b", r"\bboth\s+ways\b",
        r"\bheen\s+en\s+terug\b", r"\bretoer\s*rit\b", r"\brondrit\b", r"\bloaded\s+both\s+ways\b",
        r"\bvol\s+heen\s+en\s+terug\b", r"\bterug\s+ook\s+(?:gelaai|vol)\b", r"\bloaded\s+(?:there\s+and\s+)?back\b(?!\s+load)",
    ]
    patterns_one = [r"\b(?:1|one)\s*way\b", r"\bsingle\s+trip\b", r"\been\s*rigting\b", r"\b1\s*rigting\b",
                    r"\benkel\s*rit\b", r"\benkel\b", r"\bnet\s+heen\b", r"\bslegs\s+heen\b", r"\bone\s*direction\b"]
    patterns_rl_none = [r"\bno\s+(?:return|back)\s*load\b", r"\bgeen\s+(?:retoer|terug)\s*vrag\b",
                        r"\bsonder\s+(?:n\s+)?(?:retoer|terug)\s*vrag\b", r"\bnie\s+n\s+(?:retoer|terug)\s*vrag\b",
                        r"\bwithout\s+a\s+(?:return|back)\s*load\b", r"\bempty\s+(?:back|return|on\s+the\s+way\s+back)\b",
                        r"\b(?:back|return(?:ing)?|come\s+back|coming\s+back|running\s+back)\s+empty\b",
                        r"\bleeg\s+terug\b", r"\bterug\s+leeg\b", r"\blee\s+terug\s*(?:rit|ry)\b",
                        r"\bleeg\s+terug\s*(?:ry|rit|kom)\b", r"\bdeadhead\b", r"\bempty\s+leg\b"]
    patterns_rl_booked = [r"\b(?:retoer|terug)\s*vrag\b", r"\bvrag\s+terug\b", r"\breturn\s*load\b",
                          r"\bback\s*load\b", r"\bbackhaul\b", r"\bloaded\s+back\b"]

    def first(pats):
        for p in pats:
            m = re.search(p, t)
            if m:
                return m
        return None

    m_none = first(patterns_rl_none)
    if m_none:
        ctx.consume(m_none.start(), m_none.end())
        out.set("return_load_booked", False, 0.9)
    else:
        m_b = first(patterns_rl_booked)
        if m_b:
            ctx.consume(m_b.start(), m_b.end())
            # "booked", "sorted", "het" around it are confirmation words
            for w in re.finditer(r"\b(booked|sorted|confirmed|bespreek|gereel|organised|arranged)\b", t):
                ctx.consume(w.start(), w.end())
            out.set("return_load_booked", True, 0.85)

    m_r = first(patterns_round)
    m_o = first(patterns_one)
    if m_r and not (m_none or out.fields.get("return_load_booked")):
        ctx.consume(m_r.start(), m_r.end())
        out.set("trip_type", "ROUND_TRIP", 0.9)
    elif m_r:
        ctx.consume(m_r.start(), m_r.end())
        if m_r.group(0).startswith("loaded"):
            out.set("trip_type", "ROUND_TRIP", 0.7)
        else:
            out.flag("round trip and a return-load note both mentioned")
    if m_o:
        ctx.consume(m_o.start(), m_o.end())
        if out.fields.get("trip_type") == "ROUND_TRIP":
            out.flag("one-way and round trip both mentioned")
            out.fields.pop("trip_type", None)
            out.confidence.pop("trip_type", None)
        else:
            out.set("trip_type", "ONE_WAY", 0.95)
    if "trip_type" not in out.fields and "return_load_booked" in out.fields:
        out.set("trip_type", "ONE_WAY", 0.7)


def _border_and_international_keywords(ctx: _Ctx, out: PreParse) -> None:
    for m in _BORDER_RX.finditer(ctx.t):
        before, after = ctx.t[max(0, m.start() - 20):m.start()], ctx.t[m.end():m.end() + 5]
        # "Beitbridge to Lusaka" / "from Beitbridge": the town is the origin,
        # not a border post the truck crosses ("via/oor/through Beitbridge").
        as_place = (re.search(rf"(?:^|\s)(?:{_PICKUP_MARKERS}|{_DELIVERY_MARKERS})\s+$", before)
                    or re.match(r"\s+(?:to|na|2)\s", after)) \
            and not re.search(rf"(?:^|\s)(?:{_STOP_MARKERS})\s+$", before)
        if as_place and _PLACE_RX.match(ctx.t, m.start()):
            continue
        ctx.consume(m.start(), m.end())
        out.set("border_post", _BORDER_LOOKUP[m.group(1)], 0.95)
        out.set("international", True, 0.95)
        break
    m = re.search(r"\b(?:cross\s*border|across\s+the\s+border|oor\s*die\s+grens|oorgrens|grens\s*oorsteek|"
                  r"border\s+crossing|grenspos|border\s+post|export\s+load|uitvoer)\b", ctx.t)
    if m:
        ctx.consume(m.start(), m.end())
        out.set("international", True, 0.85)
    m = re.search(r"\b(?:local|domestic|binnelands|plaaslik)\b", ctx.t)
    if m and "international" not in out.fields:
        ctx.consume(m.start(), m.end())
        out.set("international", False, 0.8)


def _abnormal(ctx: _Ctx, out: PreParse) -> None:
    m = re.search(r"\b(?:abnormale?\s+(?:vrag|load|lading)|abnormal|abnormals|oorgrootte\s*(?:vrag)?|oor\s*grootte|"
                  r"over\s*size(?:d)?(?:\s+load)?|over\s*dimension(?:al)?|wide\s+load|bree\s+vrag|"
                  r"abnormal\s+permit|abnormale\s+permit)\b", ctx.t)
    neg = re.search(r"\b(?:not|nie|no|geen)\s+(?:n\s+|an\s+|a\s+)?(?:abnormal|abnormale)\b", ctx.t)
    if neg:
        ctx.consume(neg.start(), neg.end())
        out.set("abnormal_load", False, 0.85)
    elif m:
        ctx.consume(m.start(), m.end())
        out.set("abnormal_load", True, 0.9)


def _vehicles(ctx: _Ctx, out: PreParse, vehicle_types) -> None:
    m = _VEHICLE_RX.search(ctx.t)
    if not m:
        if out.vehicle_capacity_t:
            out.vehicle_hint, out.vehicle_hint_label = "rigid", f"{_fmt_num(out.vehicle_capacity_t)} t truck"
        else:
            return
    else:
        ctx.consume(m.start(), m.end())
        key, label = _VEHICLE_LOOKUP[m.group(1)]
        out.vehicle_hint, out.vehicle_hint_label = key, label
    if vehicle_types is None:
        return
    records = [v if isinstance(v, dict) else {"name": v, "capacity_t": None} for v in vehicle_types]
    matched = match_fleet_vehicle(out.vehicle_hint, records, out.vehicle_capacity_t)
    if matched:
        out.set("vehicle_type", matched, 0.85)
    elif records or vehicle_types == []:
        out.unmatched["vehicle_type"] = out.vehicle_hint_label


def match_fleet_vehicle(hint_key: Optional[str], records: List[Dict[str, Any]],
                        capacity_t: Optional[float] = None) -> Optional[str]:
    """Resolve a canonical vehicle hint against this company's real types.
    Body-style hints match on their alias phrases; a bare size ("8 ton
    truck") picks the smallest type whose capacity covers it."""
    if not hint_key or not records:
        return None
    from core.services.llm_quote import match_vehicle_type
    names = [r["name"] for r in records]
    if hint_key == "rigid" and capacity_t:
        covering = sorted((r.get("capacity_t"), r["name"]) for r in records
                          if r.get("capacity_t") and r["capacity_t"] >= capacity_t * 0.95
                          and not re.search(r"reefer|refrigerat|tanker|tipper|lowbed|car carrier", r["name"], re.I))
        if covering:
            return covering[0][1]
    for phrase in _VEHICLE_FLEET_PHRASES.get(hint_key, []):
        n_phrase = norm_phrase(phrase)
        for name in names:
            if re.search(rf"\b{re.escape(n_phrase)}", norm_phrase(name)):
                return name
    for phrase in _VEHICLE_FLEET_PHRASES.get(hint_key, []):
        hit = match_vehicle_type(phrase, names)
        if hit:
            return hit
    return None


def _resolve_weekday(today: date, wd: int, prefix: str) -> Tuple[date, float]:
    delta = (wd - today.weekday()) % 7
    if delta == 0:
        delta = 7
    conf = 0.85
    if prefix in ("next", "volgende") and delta <= 1:
        conf = 0.6  # "next Friday" said on a Thursday: tomorrow or a week later?
    return today + timedelta(days=delta), conf


_DELIVERY_CUE = (r"\b(?:deliver(?:ed|y|ing)?|drop(?:\s*off)?|offload|arriv(?:e|al|ing)|eta|by|before|"
                 r"no\s+later\s+than|aflewer(?:ing)?|afgelewer|aflaai|teen|voor|kom\s+aan|aankoms|"
                 r"daar\s+wees|there\s+by)\b")
_PICKUP_CUE = (r"\b(?:pick\s*up|pickup|collect(?:ion)?|load(?:ing|s|ed)?|leave|leaves|leaving|depart(?:ure|s)?|"
               r"ready|oplaai|optel|laai|haal|afhaal|vertrek|gereed|start)\b")
_VALID_CUE = r"\b(?:valid|geldig|quote\s+expires|expires)\b(?:\s+(?:until|till|tot|for|vir))?"
_POSTPOSED = re.compile(r"\s+(aflewer|afgelewer|aflaai|deliver|delivery|oplaai|laai|optel|haal|afhaal|pickup|vertrek)\b")


def _date_role(ctx: _Ctx, start: int, end: int, boundary: int) -> Tuple[Optional[str], int]:
    """Role of the date at [start, end) -> (role, new boundary).

    The nearest cue BEFORE the date (but after `boundary`, the end of the
    previous date or the verb it used) wins: "pickup Friday deliver Saturday".
    Only when there is none does an Afrikaans verb-last cue right after it
    count ("Vrydag aflewer"); that verb then belongs to this date, so the next
    date can't reuse it."""
    before = ctx.t[max(boundary, start - 45):start]
    best, best_pos = None, -1
    for role, rx in (("valid_until", _VALID_CUE), ("delivery_date", _DELIVERY_CUE), ("pickup_date", _PICKUP_CUE)):
        for m in re.finditer(rx, before):
            if m.end() > best_pos:
                best, best_pos = role, m.end()
    if best:
        return best, end
    m = _POSTPOSED.match(ctx.t[end:end + 16])
    if m:
        role = "delivery_date" if m.group(1) in ("aflewer", "afgelewer", "aflaai", "deliver", "delivery") \
            else "pickup_date"
        return role, end + m.end()
    return None, end


def _dates(ctx: _Ctx, out: PreParse, today: date) -> None:
    t = ctx.t
    hits: List[Tuple[int, int, date, float]] = []

    def add(m, d, conf, span=None):
        a, b = span or (m.start(), m.end())
        if ctx.consumed(a, b):
            return
        if d < today - timedelta(days=1) or d > today + timedelta(days=MAX_DATE_AHEAD_DAYS):
            out.flag(f"date {d.isoformat()} is out of range", "date")
            ctx.consume(a, b)
            return
        ctx.consume(a, b)
        hits.append((a, b, d, conf))

    # "valid for 7 days" / "geldig vir 7 dae" is a duration, resolved against today
    m = re.search(r"\b(?:valid|geldig)\s+(?:for|vir)\s+(\d{1,3})\s+(?:days?|dae)\b", t)
    if m and "valid_until" not in out.fields:
        ctx.consume(m.start(), m.end())
        out.set("valid_until", (today + timedelta(days=int(m.group(1)))).isoformat(), 0.9)
    for m in re.finditer(r"\b(today|vandag|tonight|vanaand)\b", t):
        add(m, today, 0.95)
    for m in re.finditer(r"\b(overmorrow|day after tomorrow|oor\s*more)\b", t):
        add(m, today + timedelta(days=2), 0.95)
    for m in re.finditer(r"\b(tomorrow|tomorrowaf|tomorow|tommorow|tmrw|tmr|2moro|2morrow)\b", t):
        add(m, today + timedelta(days=1), 0.95)
    # Afrikaans "more" typed without the circumflex: only when the message is
    # Afrikaans and it isn't the English comparative ("more than", "no more").
    for m in re.finditer(r"\bmore\s+(?:oggend|middag|aand|vroeg)\b", t):
        add(m, today + timedelta(days=1), 0.9)
    if out.language_hint == "af":
        for m in re.finditer(r"(?<!\bno )(?<!\bany )(?<!\bsome )(?<!\bmuch )\bmore\b(?!\s+(?:than|as|dan|then|of))", t):
            add(m, today + timedelta(days=1), 0.8)
    for m in re.finditer(r"\b(?:in|oor|within|binne)\s+(\d{1,3})\s+(?:days?|dae|dag)\b|\b(\d{1,3})\s+(?:days?|dae)\s+(?:from\s+now|from\s+today|van\s+nou\s+af|van\s+nou|later)\b", t):
        n = int(m.group(1) or m.group(2))
        add(m, today + timedelta(days=n), 0.9)
    for m in re.finditer(r"\b(next|volgende|this|hierdie|coming|komende|on|op)?\s*(" + "|".join(_WEEKDAYS) + r")\b", t):
        prefix = (m.group(1) or "").strip()
        d, conf = _resolve_weekday(today, _WEEKDAYS[m.group(2)], prefix)
        add(m, d, conf)
    month_rx = "|".join(sorted(_MONTHS, key=len, reverse=True))

    def ymd(y, mo, d):
        try:
            return date(y, mo, d)
        except ValueError:
            return None

    def next_occurrence(mo, d, m=None):
        """This year's date, or next year's when it is well past. A date only
        just past (<= 60 days, e.g. "2 October" said on 8 October) is a
        mistake or a typo, not next year: flagged, never filled."""
        cand = ymd(today.year, mo, d)
        if cand and cand < today:
            if (today - cand).days <= 60:
                if m is not None:
                    ctx.consume(m.start(), m.end())
                out.flag(f"{cand.day} {_MONTH_SHORT[cand.month - 1]} is in the past \u2014 which date?", "date")
                return None
            cand = ymd(today.year + 1, mo, d)
        return cand

    for m in re.finditer(rf"\b(\d{{1,2}})(?:st|nd|rd|th|ste|de)?\s+(?:of\s+)?({month_rx})\b(?:\s+(\d{{4}}))?", t):
        d = ymd(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1))) if m.group(3) else \
            next_occurrence(_MONTHS[m.group(2)], int(m.group(1)), m)
        if d:
            add(m, d, 0.95)
    for m in re.finditer(rf"\b({month_rx})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:\s+(\d{{4}}))?", t):
        if m.group(1) in ("may", "mar", "sep", "des", "mei") and not m.group(2):
            continue
        d = ymd(int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2))) if m.group(3) else \
            next_occurrence(_MONTHS[m.group(1)], int(m.group(2)), m)
        if d:
            add(m, d, 0.9)
    for m in re.finditer(r"\b(?:on\s+)?(?:the|die|op\s+die)\s+(\d{1,2})(?:st|nd|rd|th|ste|de)\b(?!\s+(?:of\s+)?(?:"
                         + month_rx + r"))", t):
        dd = int(m.group(1))
        cand = ymd(today.year, today.month, dd) if dd <= 31 else None
        if cand and cand < today:
            nm, ny = (today.month % 12) + 1, today.year + (1 if today.month == 12 else 0)
            cand = ymd(ny, nm, dd)
        if cand:
            add(m, cand, 0.8)
    for m in re.finditer(r"\b(\d{4})/(\d{1,2})/(\d{1,2})\b", t):
        d = ymd(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            add(m, d, 0.95)
    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", t):
        dd, mm = int(m.group(1)), int(m.group(2))
        yy = m.group(3)
        if mm > 12:
            continue
        if yy:
            y = int(yy) + (2000 if len(yy) == 2 else 0)
            d = ymd(y, mm, dd)
        else:
            d = next_occurrence(mm, dd, m)
        if d:
            add(m, d, 0.85)
    for m in re.finditer(r"\b(?:next|volgende|this|hierdie)\s+week\b|\b(?:end\s+of\s+(?:the\s+)?month|month\s*end|"
                         r"einde\s+van\s+die\s+maand|maandeinde)\b|\bsoon\b|\bgou\b|\basap\b", t):
        if not ctx.consumed(m.start(), m.end()):
            ctx.consume(m.start(), m.end())
            # "volgende week Woensdag" is resolved by the weekday: no question
            resolved = re.match(r"\s+(?:on\s+|op\s+)?(?:" + "|".join(_WEEKDAYS) + r")\b", t[m.end():m.end() + 16]) \
                or re.search(r"(?:" + "|".join(_WEEKDAYS) + r")\s+$", t[max(0, m.start() - 14):m.start()])
            if m.group(0) not in ("asap", "soon", "gou") and not resolved:
                out.flag(f"“{m.group(0)}” — which day?")

    if not hits:
        return
    hits.sort(key=lambda h: h[0])
    unassigned = []
    boundary = 0
    for a, b, d, conf in hits:
        role, boundary = _date_role(ctx, a, b, boundary)
        if role and role not in out.fields:
            out.set(role, d.isoformat(), conf)
        elif role and out.fields.get(role) != d.isoformat():
            out.flag(f"two {role.replace('_', ' ')}s mentioned")
        else:
            unassigned.append((d, conf))
    for d, conf in unassigned:
        for role in ("pickup_date", "delivery_date"):
            if role not in out.fields:
                out.set(role, d.isoformat(), conf * 0.85)
                break
    pd, dd = out.fields.get("pickup_date"), out.fields.get("delivery_date")
    if pd and dd and dd < pd:
        # never return a delivery before the pickup: ask instead
        out.flag("delivery date is before pickup date", "delivery_date")
        out.fields.pop("delivery_date", None)
        out.confidence.pop("delivery_date", None)


def _driver_and_fuel(ctx: _Ctx, out: PreParse) -> None:
    t = ctx.t
    m = re.search(r"\b(\d{1,2})\s+(?:nights?|nagte|nag|sleepovers?|slaapplekke)\b(?:\s+(?:out|uit|weg|away))?", t)
    if m:
        n = int(m.group(1))
        ctx.consume(m.start(), m.end())
        if 0 <= n <= MAX_DRIVER_NIGHTS:
            out.set("driver_nights", n, 0.9)
        else:
            out.flag(f"{n} driver nights looks wrong", "driver_nights")
    m = re.search(r"\b(?:diesel|fuel|brandstof|petrol)\b(?:\s+(?:price|prys))?\s*(?:at|@|is|teen|for|vir|of|van|=)?\s*"
                  r"(?:r\s*)?(\d{1,3}(?:\.\d{1,2})?)\s*(?:rand)?\s*(?:/\s*l|per\s+(?:litre|liter|l)|a\s+litre|n\s+liter|l)?\b", t)
    if m:
        v = float(m.group(1))
        ctx.consume(m.start(), m.end())
        if FUEL_PRICE_RANGE[0] <= v <= FUEL_PRICE_RANGE[1]:
            out.set("fuel_price_override", v, 0.85)
        else:
            out.flag(f"fuel price R {_fmt_num(v)}/L looks wrong", "fuel_price_override")


def _significant(name: str) -> List[str]:
    from core.services.llm_quote import _GENERIC_ENTITY_WORDS
    return [w for w in norm_phrase(name).split() if w not in _GENERIC_ENTITY_WORDS and len(w) >= 3]


def _customers(ctx: _Ctx, out: PreParse, customers) -> None:
    """Customer mention → this company's own record, matched locally."""
    from core.services.llm_quote import _fuzzy_match
    t = ctx.t
    explicit = re.search(r"\b(?:client|customer|klient|klant)\s*(?:is|will\s+be|=|:|name\s+is|se\s+naam\s+is)?\s+"
                         r"([a-z][a-z0-9&]*(?:\s+[a-z0-9&]+){0,3}?)(?=\s+(?:from|van|to|na|for|vir|with|met|on|op|"
                         r"\d)|\s*$|\s*,|\s+(?:and|en)\b)", t)
    loose = None
    if not explicit:
        loose = re.search(r"\b(?:for|vir|quote\s+for|kwotasie\s+vir)\s+([a-z][a-z0-9&]*(?:\s+[a-z0-9&]+){0,3}?)"
                          r"(?=\s+(?:from|van|to|na|with|met|on|op|\d)|\s*$|\s*,|\s+(?:and|en)\b)", t)
    m = explicit or loose
    names = [c["name"] for c in (customers or [])]
    if m:
        raw = m.group(1).strip()
        first = raw.split()[0]
        if first in _KNOWN_VOCAB or canonical_place(raw) or re.match(r"\d", first):
            m = None if not explicit else m
        if m:
            matched = _fuzzy_match(raw, names) if names else None
            if matched:
                cust = next(c for c in customers if c["name"] == matched)
                ctx.consume(m.start(), m.end())
                out.customer_span_text = raw
                out.set("customer_id", cust["id"], 0.9)
                out.set("customer_name", matched, 0.9)
                return
            if explicit and raw and first not in _KNOWN_VOCAB:
                ctx.consume(m.start(), m.end())
                out.customer_span_text = raw
                if customers is not None:
                    out.unmatched["customer_name"] = raw
                return
    # No marker: a distinguishing word of exactly one customer appears in the text.
    if customers:
        from collections import Counter
        counts = Counter(w for c in customers for w in set(_significant(c["name"])))
        for c in customers:
            for w in _significant(c["name"]):
                if counts[w] == 1 and w not in _KNOWN_VOCAB and w not in _PLACE_LOOKUP and len(w) >= 4:
                    mm = re.search(rf"\b{re.escape(w)}\b", t)
                    if mm:
                        ctx.consume(mm.start(), mm.end())
                        out.customer_span_text = w
                        out.set("customer_id", c["id"], 0.75)
                        out.set("customer_name", c["name"], 0.75)
                        return


_PLACE_PARTICLES = {"de", "la", "le", "du", "st", "kwa", "port", "ga", "e"}
_NOT_PLACE_WORDS = {"close", "near", "next", "up", "down", "back", "way", "right", "also", "them", "half", "it",
                    "is", "how", "where", "here", "there", "quick", "drive", "trip", "load", "loads", "goods"}


def _title_place(phrase: str) -> str:
    """Title-case an unrecognised place, keeping SA prefixes lower ("kwamashu"
    stays "Kwamashu"; "de aar" -> "De Aar")."""
    return " ".join(w[:1].upper() + w[1:] for w in phrase.split())


_NOT_PLACE_PRECEDERS = re.compile(r"\b(?:need|needs|want|wants|have|has|going|got|able|like|how|up|close|next|"
                                  r"due|similar|compared|according|nodig)\s*$")


def _places(ctx: _Ctx, out: PreParse) -> None:
    t = ctx.t
    mentions: List[Dict[str, Any]] = []
    for m in _PLACE_RX.finditer(t):
        if ctx.consumed(m.start(), m.end()):
            continue
        alias = m.group(1)
        mentions.append({"start": m.start(), "end": m.end(), "name": _PLACE_LOOKUP[alias], "alias": alias,
                         "conf": 0.95})
    # Unknown places / STT misspellings right after a route marker.
    marker_rx = re.compile(rf"\b({_PICKUP_MARKERS}|{_DELIVERY_MARKERS}|{_STOP_MARKERS})\s+(?=([a-z][a-z ]*))")
    for m in marker_rx.finditer(t):
        a = m.start(2)
        if any(mm["start"] <= a < mm["end"] for mm in mentions) or ctx.consumed(a, a + 1):
            continue
        if _NOT_PLACE_PRECEDERS.search(t[:m.start()]):
            continue
        words = []
        parts = m.group(2).split()[:3]
        for i, w in enumerate(parts):
            particle = w in _PLACE_PARTICLES and i + 1 < len(parts) and len(parts[i + 1]) >= 3
            if (w in _KNOWN_VOCAB and not particle) or w in _PLACE_LOOKUP or (len(w) < 3 and not particle):
                break
            words.append(w)
        if words and words[-1] in _PLACE_PARTICLES:
            words.pop()
        if not words:
            continue
        phrase = " ".join(words)
        end = a + len(phrase)
        fuzzy = difflib.get_close_matches(phrase, list(_PLACE_LOOKUP), n=1, cutoff=0.82)
        if fuzzy:
            mentions.append({"start": a, "end": end, "name": _PLACE_LOOKUP[fuzzy[0]], "alias": fuzzy[0], "conf": 0.8})
        else:
            mentions.append({"start": a, "end": end, "name": _title_place(phrase), "alias": phrase, "conf": 0.6,
                             "unknown": True})
    # Unknown origin right before "to/na" ("Thohoyandou to Giyani", "…,
    # Lichtenburg na Polokwane"), when what precedes it is a boundary or
    # already-understood text — never an ordinary word ("close to Durban").
    for m in re.finditer(r"\s(?:to|na|2)\s", t):
        if any(mm["start"] < m.start() <= mm["end"] for mm in mentions):
            continue
        toks = list(re.finditer(r"[a-z][a-z]*", t[:m.start()]))[-3:]
        words = []
        for tk in reversed(toks):
            w = tk.group(0)
            if ctx.consumed(tk.start(), tk.end()) or w in _KNOWN_VOCAB or w in _NOT_PLACE_WORDS \
                    or any(mm["start"] <= tk.start() < mm["end"] for mm in mentions):
                break
            if len(w) < 3 and not (w in _PLACE_PARTICLES and words):
                break
            words.insert(0, tk)
            if len(words) == 2:
                break
        if not words:
            continue
        a, b = words[0].start(), words[-1].end()
        prev = t[:a].rstrip()
        prev_tok = re.search(r"([a-z0-9]+)\W*$", prev)
        boundary = (not prev or prev.endswith(",") or ctx.consumed(len(prev) - 1, len(prev))
                    or (prev_tok and prev_tok.group(1) in _KNOWN_VOCAB))
        if not boundary:
            continue
        phrase = t[a:b]
        fuzzy = difflib.get_close_matches(phrase, list(_PLACE_LOOKUP), n=1, cutoff=0.82)
        if fuzzy:
            mentions.append({"start": a, "end": b, "name": _PLACE_LOOKUP[fuzzy[0]], "alias": fuzzy[0], "conf": 0.75})
        else:
            mentions.append({"start": a, "end": b, "name": _title_place(phrase), "alias": phrase, "conf": 0.6,
                             "unknown": True})
    # STT misspelling anywhere (long single words only, strict cutoff).
    for m in re.finditer(r"\b[a-z]{7,}\b", t):
        if ctx.consumed(m.start(), m.end()) or any(mm["start"] <= m.start() < mm["end"] for mm in mentions):
            continue
        if m.group(0) in _KNOWN_VOCAB:
            continue
        fuzzy = difflib.get_close_matches(m.group(0), list(_SINGLE_WORD_PLACE_ALIASES), n=1, cutoff=0.88)
        if fuzzy:
            mentions.append({"start": m.start(), "end": m.end(), "name": _SINGLE_WORD_PLACE_ALIASES[fuzzy[0]],
                             "alias": fuzzy[0], "conf": 0.75})
    mentions.sort(key=lambda x: x["start"])

    # Roles from the words right before (and "toe" right after) each mention.
    deliv_rx = re.compile(rf"(?:^|\s)(?:{_DELIVERY_MARKERS})\s+(?:(?:the|die|n|a|our|ons)\s+)?$")
    pick_rx = re.compile(rf"(?:^|\s)(?:{_PICKUP_MARKERS})\s+(?:(?:the|die|n|a|our|ons)\s+)?$")
    stop_rx = re.compile(rf"(?:^|\s)(?:{_STOP_MARKERS})\s+(?:(?:the|die|n|a)\s+)?$")
    kept = []
    for mm in mentions:
        before = t[max(0, mm["start"] - 40):mm["start"]]
        after = t[mm["end"]:mm["end"] + 5]
        role = None
        if kept and kept[-1]["role"] == "stop" and re.search(r"(?:\ben|\band|,)\s*$", before) \
                and t[kept[-1]["end"]:mm["start"]].strip(" ,") in ("en", "and", ""):
            role = "stop"  # "oor Beaufort-Wes en Worcester"
        elif stop_rx.search(before):
            role = "stop"
        elif pick_rx.search(before):
            role = "pickup"
        elif deliv_rx.search(before) or re.match(r"\s+toe\b", after):
            role = "delivery"
        if mm["alias"] in _AMBIGUOUS_ALIASES and role is None and re.match(r"\s+(?:to|na|2)\s+\w", after + t[mm["end"] + 5:mm["end"] + 8]) \
                and len(mentions) >= 2:
            role = "pickup"
        if mm["alias"] in _AMBIGUOUS_ALIASES and role is None and mm["conf"] >= 0.95:
            # e.g. "George" the person, "el" the article — need a route marker
            if not (len(mentions) >= 2 and mm["alias"] in ("bloem", "potch", "pe", "joeys", "durbs", "gabs", "zim", "moz")):
                continue
        mm["role"] = role
        kept.append(mm)
    if not kept:
        return
    for mm in kept:
        ctx.consume(mm["start"], mm["end"])

    pickups = [m for m in kept if m["role"] == "pickup"]
    delivs = [m for m in kept if m["role"] == "delivery"]
    stops = [m for m in kept if m["role"] == "stop"]
    free = [m for m in kept if m["role"] is None]

    pickup = pickups[0] if pickups else None
    delivery = delivs[-1] if delivs else None
    stops = pickups[1:] + delivs[:-1] + stops
    pos_conf_penalty = 0.0
    if pickup is None and free:
        pickup = free.pop(0)
        pos_conf_penalty = 0.15
    if delivery is None and free:
        delivery = free.pop(-1)
    stops += free  # anything left in the middle of a route is a stop
    stops.sort(key=lambda x: x["start"])

    def said(mm):
        if mm.get("unknown"):
            return None
        s_ = said_text(ctx.raw, mm["alias"])
        return s_ if s_ and norm_phrase(s_) != norm_phrase(mm["name"]) else None

    if pickup:
        c = pickup["conf"] - (pos_conf_penalty if pickup["role"] is None else 0)
        out.set("pickup_location", pickup["name"], c)
        if said(pickup):
            out.said["pickup_location"] = said(pickup)
    if delivery:
        c = delivery["conf"] - (0.15 if delivery["role"] is None else 0)
        out.set("delivery_location", delivery["name"], c)
        if said(delivery):
            out.said["delivery_location"] = said(delivery)
    names = [s["name"] for s in stops if s["name"] not in (out.fields.get("pickup_location"),
                                                            out.fields.get("delivery_location"))]
    if names:
        out.set("stops", names[:8], min(s["conf"] for s in stops) - 0.1)
    if pickup and delivery and pickup["name"] == delivery["name"] and pickup["name"] not in _COUNTRY_NAMES:
        out.flag("pickup and delivery are the same place")
    for mm in kept:
        if mm.get("unknown"):
            out.flag(f"place “{mm['name']}” not recognised — check it on the map")
    out._places_meta = kept  # type: ignore[attr-defined]



# Words that end a cargo noun phrase after a weight ("28 ton steel coils from …").
_CARGO_STOP = set("""
from frm van vanaf uit to na naar tot via oor deur through on op in at by for vir with met and en or of en
the die a an n tomorrow today tonight vandag overmorrow tomorrowaf more next volgende this hierdie
please asseblief pls asb round one way heen return retoer empty leeg back terug
""".split())


def _cargo_after_weight(ctx: _Ctx, out: PreParse) -> None:
    """The noun phrase right after the load weight is the cargo, said in full:
    "22 ton of paper rolls", "5 ton avocados", "45 ton transformer". A phrase
    that is exactly a known (Afrikaans/English) cargo word gets its English
    name ("staalrolle" -> steel coils); anything longer or unknown is kept as
    the user said it, so "chicken feed" never shrinks to "chicken"."""
    t = ctx.t
    for end in ctx.weight_ends:
        m = re.match(r"\s+(?:of\s+|worth\s+of\s+|aan\s+)?((?:[a-z][a-z.]*\s*){1,3})", t[end:])
        if not m:
            continue
        a = end + m.start(1)
        words, pos = [], a
        for w in m.group(1).split():
            wa = t.index(w, pos)
            if w in _CARGO_STOP or w in _PLACE_LOOKUP or _PLACE_RX.match(t, wa) or ctx.consumed(wa, wa + len(w)) \
                    or re.fullmatch(_VEHICLE_NOUNS, w) or w in _WEEKDAYS or w in _MONTHS \
                    or (w in _KNOWN_VOCAB and w not in _CARGO_WORDS):
                break
            words.append(w)
            pos = wa + len(w)
        # a final word that starts a route ("… transformer Majuba to Ankerlig") is a place
        if words and re.match(r"\s+(?:to|na|2)\s", t[pos:pos + 5]) and len(words) > 1:
            words.pop()
            pos = t.rindex(words[-1], a, pos) + len(words[-1])
        if not words:
            continue
        phrase = " ".join(words)
        if phrase in _CARGO_LOOKUP:
            name, conf = _CARGO_LOOKUP[phrase], 0.9
        elif all(w in _CARGO_WORDS for w in words):
            hit = _CARGO_RX.search(phrase)
            name, conf = (_CARGO_LOOKUP[hit.group(1)] if hit else phrase), 0.85
        else:
            name, conf = phrase, 0.75
        if name == "pallets":
            continue  # a pallet count/load, the goods may be named elsewhere
        ctx.consume(a, pos)
        out.set("cargo_description", name, conf)
        return


def _cargo(ctx: _Ctx, out: PreParse) -> None:
    t = ctx.t
    if "cargo_description" in out.fields:
        for h in _CARGO_RX.finditer(t):  # pallets etc. said elsewhere are understood
            if not ctx.consumed(h.start(), h.end()) and _CARGO_LOOKUP[h.group(1)] == "pallets":
                ctx.consume(h.start(), h.end())
        return
    hits = [m for m in _CARGO_RX.finditer(t) if not ctx.consumed(m.start(), m.end())]
    if hits:
        goods = [h for h in hits if _CARGO_LOOKUP[h.group(1)] != "pallets"]
        chosen = goods[0] if goods else hits[0]
        for h in hits:
            ctx.consume(h.start(), h.end())
        name = _CARGO_LOOKUP[chosen.group(1)]
        if goods and any(_CARGO_LOOKUP[h.group(1)] == "pallets" for h in hits):
            name = f"{name} on pallets"
        out.set("cargo_description", name, 0.9)
        return
    m = re.search(r"\b(?:of|load\s+of|loads\s+of)\s+([a-z][a-z ]{2,30}?)(?=\s+(?:from|van|to|na|on|op|for|vir|by|"
                  r"via|with|met|\d)|\s*$|\s*,)", t)
    if m:
        words = [w for w in m.group(1).split() if w not in ("tons", "ton", "tonnes", "kg", "kilos", "pallets",
                                                          "units", "loads", "crates")]
        if words and words[0] not in _KNOWN_VOCAB and not canonical_place(" ".join(words)):
            ctx.consume(m.start(1), m.end(1))
            out.set("cargo_description", " ".join(words), 0.6)


def _international_from_places(out: PreParse) -> None:
    countries = set()
    for key in ("pickup_location", "delivery_location"):
        cc = _PLACE_COUNTRY.get(out.fields.get(key) or "")
        if cc:
            countries.add(cc)
    for s in out.fields.get("stops") or []:
        cc = _PLACE_COUNTRY.get(s)
        if cc:
            countries.add(cc)
    if countries - {"ZA"}:
        out.set("international", True, 0.95)
    elif countries == {"ZA"} and out.fields.get("pickup_location") and out.fields.get("delivery_location") \
            and "international" not in out.fields:
        both_known = all(_PLACE_COUNTRY.get(out.fields[k]) for k in ("pickup_location", "delivery_location"))
        if both_known:
            out.set("international", False, 0.85)


def _distance_mentions(ctx: _Ctx, out: Optional[PreParse] = None) -> None:
    for m in re.finditer(r"\b(\d+(?:\.\d+)?)\s*(?:litres?|liters?|l|kl|kilolitres?)\b", ctx.t):
        if not ctx.consumed(m.start(), m.end()):
            ctx.consume(m.start(), m.end())
            if out is not None and "weight" not in out.fields:
                out.flag(f"volume {m.group(1)} L given \u2014 what does it weigh?", "weight_missing")
    for m in re.finditer(r"\b\d+(?:\.\d+)?\s*(?:km|kms|kilometres?|kilometers?|kilometer)\b", ctx.t):
        ctx.consume(m.start(), m.end())


def _residue(ctx: _Ctx, out: PreParse) -> None:
    left = []
    for m in re.finditer(r"[a-z0-9.]+", ctx.t):
        if ctx.consumed(m.start(), m.end()):
            continue
        w = m.group(0)
        if w in _FILLER or w in _KNOWN_VOCAB and w not in ("client", "customer", "klient", "diesel", "fuel"):
            continue
        if re.fullmatch(r"\d+(?:\.\d+)?", w):
            # an unexplained number is exactly what an LLM (or the user) must look at
            left.append(w)
            continue
        if len(w) <= 1:
            continue
        left.append(w)
    out.residue = left
    nums = [w for w in left if re.fullmatch(r"\d+(?:\.\d+)?", w)]
    if nums and "weight" not in out.fields:
        out.flag(f"number {nums[0]} — tons or kg?")
