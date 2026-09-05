"""Safety filter — CLAUDE.md pipeline stage 1 (hard rule, never ML).

Removes menu items that conflict with a user's allergies / avoidances / diet
BEFORE anything else touches the candidate pool. Pure deterministic rule
matching against ingredient text and the API-supplied diet tags. This module
imports nothing from the ML / clustering / ranking code and nothing from the
rest of ``food_pipeline`` — it is standalone by design, so a clustering or
ranking model can never be the sole safeguard against an allergen.

It ALWAYS runs: with no restrictions it excludes zero items (it does not skip).

Decision per (item, restriction):
  1. scan the ingredient names for the restriction's keywords;
  2. if any ingredient matches            -> EXCLUDE (a concrete match wins,
                                              even over a "free-from" tag);
  3. else if a diet tag clears it         -> keep;
  4. else if the item has ingredient data -> keep (no evidence of a conflict);
  5. else (no ingredients, no tag):
        - allergy / avoidance -> EXCLUDE as unverifiable (conservative);
        - diet                -> keep, but record "undetermined" + log.

Known limitation (see docs/phase4-safety-filter.md): matching is on ingredient
text + known tag fields only. It can miss allergens phrased unusually or
hidden inside a compound ingredient. Anyone with a serious allergy must still
check ingredients themselves — this is not a sole safeguard.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Iterable

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# data model
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class MenuItem:
    menu_id: str
    name: str
    ingredients: tuple[str, ...] = ()
    diet_tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Restrictions:
    allergies: tuple[str, ...] = ()   # e.g. ("nut", "shellfish")
    avoid: tuple[str, ...] = ()       # e.g. ("pork", "beef")
    diet: str | None = None           # e.g. "vegan"

    @property
    def is_empty(self) -> bool:
        return not self.allergies and not self.avoid and not self.diet


@dataclass(frozen=True)
class Exclusion:
    menu_id: str
    name: str
    rule: str      # "allergy:nut" | "avoid:pork" | "diet:vegan" | "unverifiable:nut"
    reason: str    # human-readable: which ingredient(s) triggered it


@dataclass(frozen=True)
class SafetyResult:
    kept: tuple[MenuItem, ...]
    excluded: tuple[Exclusion, ...]
    undetermined: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    @property
    def excluded_count(self) -> int:
        return len(self.excluded)

    @property
    def kept_count(self) -> int:
        return len(self.kept)


# --------------------------------------------------------------------------
# keyword tables  (all lower-case; matched as whole words unless noted)
# --------------------------------------------------------------------------
# allergen -> (positive keywords, suppressor keywords that cancel a match on
# the same ingredient string, e.g. "coconut" cancels a bare "nut" hit).
_ALLERGENS: dict[str, tuple[set[str], set[str]]] = {
    "nut": (
        {
            "peanut", "peanuts", "groundnut", "groundnuts", "monkey nut",
            "almond", "almonds", "cashew", "cashews", "walnut", "walnuts",
            "pecan", "pecans", "hazelnut", "hazelnuts", "filbert",
            "pistachio", "pistachios", "macadamia", "brazil nut", "pine nut",
            "pine nuts", "chestnut", "nut butter", "nut oil", "mixed nuts",
            "chopped nuts", "ground nuts", "praline", "marzipan", "nougat",
            "gianduja", "frangipane", "nutella", "nut paste", "nut meal",
            "nut flour", "amaretto", "orgeat", "nuts",
        },
        # not tree nuts even though the text contains "nut"
        {"coconut", "nutmeg", "butternut", "water chestnut", "doughnut",
         "donut", "nutritional yeast", "nut-free", "nut free"},
    ),
    "shellfish": (
        {
            "shrimp", "prawn", "prawns", "crab", "lobster", "crayfish",
            "crawfish", "langoustine", "scampi", "clam", "clams", "mussel",
            "mussels", "oyster", "oysters", "scallop", "scallops", "cockle",
            "whelk", "abalone", "squid", "calamari", "octopus", "cuttlefish",
            "escargot", "snail", "krill", "shellfish", "seafood",
        },
        {"mock crab", "imitation crab", "crab apple"},
    ),
    "dairy": (
        {
            "milk", "cream", "creamy", "butter", "buttery", "cheese", "cheesy",
            "ranch", "yogurt", "yoghurt", "whey",
            "casein", "caseinate", "ghee", "buttermilk", "custard", "curd",
            "paneer", "mascarpone", "ricotta", "parmesan", "parmigiano",
            "mozzarella", "gouda", "cheddar", "brie", "feta", "gruyere",
            "havarti", "provolone", "camembert", "quark", "kefir", "clotted",
            "creme fraiche", "half and half", "half-and-half", "milk powder",
            "milk solids", "condensed milk", "evaporated milk", "sour cream",
            "ice cream", "gelato", "milkfat", "lactose",
        },
        {
            "coconut milk", "coconut cream", "almond milk", "soy milk",
            "oat milk", "rice milk", "cashew milk", "peanut butter",
            "cocoa butter", "shea butter", "apple butter", "nut butter",
            "almond butter", "sunflower butter", "dairy-free", "dairy free",
            "non-dairy", "nondairy", "butternut", "butterhead", "butter bean",
            "butter lettuce", "cream of tartar", "creamed corn",
        },
    ),
    "egg": (
        {
            "egg", "eggs", "egg white", "egg whites", "egg yolk", "egg yolks",
            "albumen", "albumin", "mayonnaise", "mayo", "aioli", "meringue",
            "egg noodle", "egg noodles", "egg wash", "powdered egg",
            "dried egg",
        },
        {"eggplant", "egg-free", "egg free", "egg substitute", "egg replacer"},
    ),
    "soy": (
        {
            "soy", "soya", "soybean", "soybeans", "soy sauce", "shoyu",
            "tamari", "edamame", "tofu", "tempeh", "miso", "natto",
            "soy lecithin", "soy protein", "textured vegetable protein",
            "tvp", "soy milk", "soybean oil", "soy flour",
        },
        {"soy-free", "soy free"},
    ),
    "gluten": (
        {
            "wheat", "flour", "barley", "rye", "malt", "malted", "bulgur",
            "couscous", "semolina", "farro", "spelt", "kamut", "triticale",
            "seitan", "breadcrumbs", "bread crumbs", "panko", "bread",
            "pasta", "noodles", "cracker", "crackers", "beer", "ale",
            "graham", "roux", "soy sauce", "wheat starch", "durum",
            "orzo", "matzo", "matzah", "vital wheat gluten", "gluten",
        },
        {
            "almond flour", "coconut flour", "rice flour", "corn flour",
            "cornflour", "chickpea flour", "gram flour", "tapioca flour",
            "buckwheat", "gluten-free", "gluten free", "gf ", "rice noodles",
            "glass noodles", "cellophane noodles", "corn tortilla",
            "potato starch", "arrowroot", "oat flour",
        },
    ),
    "fish": (
        {
            "fish", "salmon", "tuna", "cod", "halibut", "trout", "bass",
            "tilapia", "snapper", "mackerel", "sardine", "sardines",
            "anchovy", "anchovies", "herring", "haddock", "pollock",
            "catfish", "sole", "flounder", "grouper", "mahi", "swordfish",
            "fish sauce", "fish stock", "nam pla", "worcestershire",
            "caesar dressing", "surimi", "bonito", "dashi", "roe", "caviar",
            "gravlax", "lox", "kipper",
        },
        {"jellyfish", "fish-free", "fish free", "shellfish"},
    ),
    "sesame": (
        {
            "sesame", "tahini", "tahina", "sesame oil", "sesame seed",
            "sesame seeds", "benne", "halva", "halvah", "gomashio",
            "za'atar", "zaatar", "hummus", "houmous",
        },
        {"sesame-free", "sesame free"},
    ),
}

# avoidance key -> (positive keywords, suppressors)
_AVOIDANCES: dict[str, tuple[set[str], set[str]]] = {
    "pork": (
        {
            "pork", "bacon", "ham", "prosciutto", "pancetta", "lard",
            "chorizo", "salami", "pepperoni", "gammon", "speck", "guanciale",
            "carnitas", "spare ribs", "chicharron", "mortadella", "capicola",
            "coppa", "hock", "pig", "swine", "pulled pork",
        },
        {"turkey bacon", "beef bacon", "chicken sausage", "beef sausage",
         "vegan bacon", "vegetarian bacon", "pork-free", "turkey ham",
         "beef salami", "turkey pepperoni"},
    ),
    "beef": (
        {
            "beef", "steak", "brisket", "corned beef", "pastrami", "veal",
            "beef stock", "beef broth", "oxtail", "prime rib", "sirloin",
            "ribeye", "rib eye", "filet mignon", "tri-tip", "chuck roast",
            "beef jerky", "ground beef", "minced beef", "hamburger",
            "beef mince", "cow", "bovine",
        },
        {"beef-free", "impossible beef", "beyond beef", "mushroom steak",
         "cauliflower steak", "vegan beef"},
    ),
    "poultry": (
        {"chicken", "turkey", "duck", "goose", "quail", "poultry", "hen",
         "capon", "pheasant", "chicken stock", "chicken broth"},
        {"chicken-free", "vegan chicken", "chickenless", "chickpea",
         "mock chicken", "chicken of the woods"},
    ),
    "alcohol": (
        {"wine", "beer", "ale", "rum", "brandy", "vodka", "whiskey",
         "whisky", "bourbon", "liqueur", "sherry", "port wine", "sake",
         "mirin", "vermouth", "kirsch", "cognac", "marsala", "tequila",
         "champagne", "cooking wine", "rice wine"},
        {"non-alcoholic", "alcohol-free", "ginger beer", "root beer",
         "wine vinegar", "rice wine vinegar", "red wine vinegar",
         "white wine vinegar"},
    ),
    "gelatin": ({"gelatin", "gelatine", "isinglass"}, {"agar", "pectin"}),
    "honey": ({"honey"}, {"honeydew", "honey-free", "honeycomb pattern"}),
}

# diet -> which restriction keys it forbids (reused from the tables above),
# plus extra keywords specific to the diet.
_DIET_FORBIDS: dict[str, dict[str, object]] = {
    "vegan": {
        "meat": True, "fish": True, "shellfish": True, "dairy": True,
        "egg": True, "honey": True, "gelatin": True,
        "extra": {"lard", "tallow", "suet", "rennet", "carmine", "shellac",
                  "bone broth", "duck fat", "chicken fat", "schmaltz"},
    },
    "vegetarian": {
        "meat": True, "fish": True, "shellfish": True,
        "extra": {"lard", "tallow", "suet", "gelatin", "rennet",
                  "bone broth", "anchovy", "fish sauce", "duck fat",
                  "chicken fat", "schmaltz"},
    },
    "pescatarian": {
        "meat": True,
        "extra": {"lard", "tallow", "suet", "duck fat", "chicken fat",
                  "schmaltz", "bone broth"},
    },
    "halal": {
        "pork": True, "alcohol": True,
        "extra": {"gelatin", "gelatine", "lard", "bacon", "ham", "blood sausage"},
    },
    "kosher": {
        "pork": True, "shellfish": True,
        "extra": {"gelatin", "gelatine", "lard", "blood sausage", "catfish",
                  "rabbit"},
    },
}

# generic meat keywords for diet checks
_MEAT_KEYWORDS: set[str] = {
    "beef", "steak", "veal", "pork", "bacon", "ham", "lamb", "mutton",
    "goat", "venison", "bison", "buffalo", "rabbit", "boar", "elk",
    "chicken", "turkey", "duck", "goose", "quail", "poultry", "sausage",
    "meatball", "meatballs", "mince", "ground meat", "brisket", "sirloin",
    "prosciutto", "pancetta", "chorizo", "salami", "pepperoni", "pastrami",
    "corned beef", "hot dog", "bratwurst", "kielbasa", "liver", "kidney",
    "tripe", "foie gras", "game hen", "cornish hen",
}

# diet -> set of API diet tags that positively clear the diet
_DIET_CLEARING_TAGS: dict[str, set[str]] = {
    "vegan": {"vegan"},
    "vegetarian": {"vegan", "vegetarian", "lacto ovo vegetarian",
                   "lacto vegetarian", "ovo vegetarian"},
    "pescatarian": {"vegan", "vegetarian", "pescatarian"},
}
# allergy -> API "free-from" tag that clears it
_ALLERGY_CLEARING_TAGS: dict[str, set[str]] = {
    "dairy": {"dairy free"},
    "gluten": {"gluten free"},
}

# diets that cannot be fully verified from ingredient text alone
_DIET_PARTIAL_NOTE = {
    "halal": "halal certification and slaughter method cannot be verified from ingredient text",
    "kosher": "kosher certification and meat/dairy separation cannot be verified from ingredient text",
}

_ALLERGY_ALIASES: dict[str, str] = {
    "peanut": "nut", "peanuts": "nut", "groundnut": "nut", "groundnuts": "nut",
    "ground nut": "nut", "ground nuts": "nut", "tree nut": "nut",
    "tree nuts": "nut", "nuts": "nut", "nut": "nut",
    "shellfish": "shellfish", "crustacean": "shellfish",
    "crustaceans": "shellfish", "molluscs": "shellfish", "shrimp": "shellfish",
    "milk": "dairy", "dairy": "dairy", "lactose": "dairy", "casein": "dairy",
    "egg": "egg", "eggs": "egg",
    "soy": "soy", "soya": "soy", "soybean": "soy", "soybeans": "soy",
    "gluten": "gluten", "wheat": "gluten", "celiac": "gluten",
    "coeliac": "gluten",
    "fish": "fish", "finfish": "fish",
    "sesame": "sesame",
}

_WORD_RE_CACHE: dict[str, re.Pattern] = {}


def _kw_pattern(keyword: str) -> re.Pattern:
    p = _WORD_RE_CACHE.get(keyword)
    if p is None:
        # whole-word-ish: keyword bounded by non-letters or string ends;
        # allows the keyword to be a phrase with spaces.
        p = re.compile(r"(?<![a-z])" + re.escape(keyword) + r"(?![a-z])")
        _WORD_RE_CACHE[keyword] = p
    return p


def _text_has(text: str, keyword: str) -> bool:
    return bool(_kw_pattern(keyword).search(text))


# --------------------------------------------------------------------------
# normalisation
# --------------------------------------------------------------------------
def normalize_allergy(raw: str) -> str:
    s = raw.strip().lower()
    s = re.sub(r"^(no |without |free of |avoid )", "", s)
    s = re.sub(r"[- ]?(allergy|allergen|free)$", "", s).strip()
    return _ALLERGY_ALIASES.get(s, s)


def normalize_avoid(raw: str) -> str:
    s = raw.strip().lower()
    s = re.sub(r"^(no |without |avoid |not? )", "", s)
    s = re.sub(r"[- ]?free$", "", s).strip()
    aliases = {
        "pig": "pork", "swine": "pork", "ham": "pork", "bacon": "pork",
        "cow": "beef", "veal": "beef",
        "chicken": "poultry", "turkey": "poultry", "duck": "poultry",
        "bird": "poultry", "fowl": "poultry",
        "booze": "alcohol", "liquor": "alcohol", "wine": "alcohol",
    }
    return aliases.get(s, s)


def parse_restrictions(
    allergies: Iterable[str] | None = None,
    diet: str | None = None,
    avoid: Iterable[str] | None = None,
) -> Restrictions:
    """Build a normalised :class:`Restrictions` from loose user input.

    ``allergies`` items that look like "no pork" are routed to ``avoid``.
    """
    norm_allergies: list[str] = []
    norm_avoid: list[str] = [normalize_avoid(a) for a in (avoid or [])]
    for raw in allergies or []:
        key = normalize_allergy(raw)
        if key in _ALLERGENS:
            norm_allergies.append(key)
        elif normalize_avoid(raw) in _AVOIDANCES:
            norm_avoid.append(normalize_avoid(raw))
        else:
            norm_allergies.append(key)  # keep as custom literal
            logger.info(
                "safety_filter: unknown allergen %r — literal substring match only", raw
            )
    d = (diet or "").strip().lower() or None
    if d and d not in _DIET_FORBIDS and d not in _DIET_CLEARING_TAGS:
        logger.info("safety_filter: unknown diet %r — cannot determine, will not guess", d)
        d = None
    return Restrictions(
        allergies=tuple(dict.fromkeys(norm_allergies)),
        avoid=tuple(dict.fromkeys(norm_avoid)),
        diet=d,
    )


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------
def _scan(ingredients: Iterable[str], positives: set[str], suppressors: set[str]) -> list[str]:
    """Return the ingredient strings that positively match and are not
    cancelled by a suppressor in the same string."""
    hits: list[str] = []
    for ing in ingredients:
        low = ing.strip().lower()
        if not low:
            continue
        if any(_text_has(low, s) for s in suppressors):
            continue
        if any(_text_has(low, p) for p in positives):
            hits.append(ing.strip())
    return hits


def _custom_scan(ingredients: Iterable[str], token: str) -> list[str]:
    return [i.strip() for i in ingredients if _text_has(i.strip().lower(), token)]


def _diet_violations(item: MenuItem, diet: str) -> list[str]:
    spec = _DIET_FORBIDS.get(diet)
    if spec is None:
        return []
    hits: list[str] = []
    if spec.get("meat"):
        hits += _scan(item.ingredients, _MEAT_KEYWORDS, set())
    for key in ("pork", "beef", "poultry", "alcohol"):
        if spec.get(key):
            pos, sup = _AVOIDANCES[key]
            hits += _scan(item.ingredients, pos, sup)
    for allergen in ("dairy", "egg", "fish", "shellfish"):
        if spec.get(allergen):
            pos, sup = _ALLERGENS[allergen]
            hits += _scan(item.ingredients, pos, sup)
    extra = spec.get("extra") or set()
    if extra:
        hits += _scan(item.ingredients, set(extra), set())
    if spec.get("honey"):
        pos, sup = _AVOIDANCES["honey"]
        hits += _scan(item.ingredients, pos, sup)
    if spec.get("gelatin"):
        pos, sup = _AVOIDANCES["gelatin"]
        hits += _scan(item.ingredients, pos, sup)
    # de-dupe, keep order
    return list(dict.fromkeys(hits))


def _evaluate_item(item: MenuItem, r: Restrictions) -> tuple[bool, Exclusion | None, str | None]:
    """Returns (excluded, Exclusion|None, undetermined_note|None)."""
    tags = {t.strip().lower() for t in item.diet_tags}
    has_ingredients = any(i.strip() for i in item.ingredients)

    # ---- allergies (medical: conservative on unknown) ----
    for allergen in r.allergies:
        if allergen in _ALLERGENS:
            pos, sup = _ALLERGENS[allergen]
            hits = _scan(item.ingredients, pos, sup)
        else:
            hits = _custom_scan(item.ingredients, allergen)
        if hits:
            return True, Exclusion(
                item.menu_id, item.name, f"allergy:{allergen}",
                f"ingredient(s) match {allergen}: {', '.join(hits[:4])}",
            ), None
        if not has_ingredients and not (tags & _ALLERGY_CLEARING_TAGS.get(allergen, set())):
            return True, Exclusion(
                item.menu_id, item.name, f"unverifiable:{allergen}",
                f"no ingredient data to rule out {allergen}",
            ), None

    # ---- avoidances ----
    for key in r.avoid:
        if key in _AVOIDANCES:
            pos, sup = _AVOIDANCES[key]
            hits = _scan(item.ingredients, pos, sup)
        else:
            hits = _custom_scan(item.ingredients, key)
        if hits:
            return True, Exclusion(
                item.menu_id, item.name, f"avoid:{key}",
                f"ingredient(s) match {key}: {', '.join(hits[:4])}",
            ), None
        if not has_ingredients:
            return True, Exclusion(
                item.menu_id, item.name, f"unverifiable:{key}",
                f"no ingredient data to rule out {key}",
            ), None

    # ---- diet ----
    note = None
    if r.diet:
        if tags & _DIET_CLEARING_TAGS.get(r.diet, set()):
            pass  # API tag positively clears the diet
        else:
            hits = _diet_violations(item, r.diet)
            if hits:
                return True, Exclusion(
                    item.menu_id, item.name, f"diet:{r.diet}",
                    f"not {r.diet}: {', '.join(hits[:5])}",
                ), None
            if not has_ingredients:
                note = f"{item.menu_id}: cannot determine {r.diet} — no ingredient data or diet tag"
                logger.info("safety_filter: %s", note)
        if r.diet in _DIET_PARTIAL_NOTE and not (tags & _DIET_CLEARING_TAGS.get(r.diet, set())):
            extra_note = f"{item.menu_id}: {_DIET_PARTIAL_NOTE[r.diet]}"
            logger.info("safety_filter: %s", extra_note)
            note = note or extra_note

    return False, None, note


# --------------------------------------------------------------------------
# public entry point
# --------------------------------------------------------------------------
def apply_safety_filter(
    items: Iterable[MenuItem], restrictions: Restrictions
) -> SafetyResult:
    """Stage 1. Always runs. Returns kept items, exclusions (with reasons),
    and any diet 'undetermined' notes. With empty restrictions,
    ``excluded_count == 0`` and every item is kept."""
    items = list(items)
    if restrictions.is_empty:
        logger.info("safety_filter: no restrictions — 0 excluded (filter still ran, %d items)", len(items))
        return SafetyResult(kept=tuple(items), excluded=(), undetermined=())

    kept: list[MenuItem] = []
    excluded: list[Exclusion] = []
    undetermined: list[tuple[str, str]] = []
    for item in items:
        is_excluded, exclusion, note = _evaluate_item(item, restrictions)
        if is_excluded and exclusion is not None:
            excluded.append(exclusion)
        else:
            kept.append(item)
            if note:
                undetermined.append((item.menu_id, note))

    logger.info(
        "safety_filter: %d in -> %d kept, %d excluded "
        "(allergies=%s avoid=%s diet=%s)",
        len(items), len(kept), len(excluded),
        list(restrictions.allergies), list(restrictions.avoid), restrictions.diet,
    )
    return SafetyResult(
        kept=tuple(kept), excluded=tuple(excluded), undetermined=tuple(undetermined)
    )


def menu_item_from_row(row: dict) -> MenuItem:
    """Build a :class:`MenuItem` from a ``menu_catalog`` row dict."""
    return MenuItem(
        menu_id=str(row.get("menu_id") or ""),
        name=str(row.get("name") or ""),
        ingredients=tuple(str(i) for i in (row.get("ingredients") or [])),
        diet_tags=tuple(str(t) for t in (row.get("diet_tags") or [])),
    )
