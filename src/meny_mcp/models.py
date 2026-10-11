"""Domain models.

Field names and types were taken from live responses on 2026-08-15:
  * search hit ``contentData._source`` -> 48 fields
  * ``/api/products/{chain}/{store}/{ean}`` -> those 48 plus ~45 more

Everything is ``Optional`` with a default. The platform is undocumented and
omits keys freely depending on product type (weight goods, alcohol, medicine,
fresh produce all differ), so strictness here would only cause spurious
failures. ``model_config`` keeps unknown keys instead of discarding them.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from .config import COMPARABLE_UNIT_GROUPS, COMPARABLE_UNIT_MAP

# Multi-packs: "2STK", "2stk", "4 stk", "2pk", "2-pk", "3ER", "2x100g".
_PIECES_RE = re.compile(r"(\d+)\s*-?\s*(?:stk|pk|er\b|x\s*\d)", re.IGNORECASE)
# Single loose items: "stykk", "pr stk", "Pr stk, First price". Matched only
# when no numbered form is present. `\b` keeps this off "stykker", "biffstykke".
_SINGLE_RE = re.compile(r"\b(?:pr\.?\s*)?(?:stk|stykk)\b", re.IGNORECASE)
# A pack whose size is stated as a mass or volume ("1KG", "700G", "500 ml") is
# a bag or a bottle, not a countable multi-pack.
_BULK_PACK_RE = re.compile(r"^\s*\d+(?:[.,]\d+)?\s*(kg|g|gram|ml|cl|dl|l)\b",
                           re.IGNORECASE)


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


# ---------------------------------------------------------------------------
# Promotions
# ---------------------------------------------------------------------------
class Promotion(_Base):
    """An active offer attached to a product.

    Note: not every entry is a price cut. Meny also ships editorial tags such
    as "Topp 100 bestselgere" through this array, so always check
    ``promoCalcPricePerUnit`` / ``status`` before treating one as a discount.
    """

    promotion_id: str | int | None = Field(None, alias="promotionId")
    name: str | None = None
    promo_name: str | None = Field(None, alias="promoName")
    # Observed as integers on live data (e.g. type=2, status=1), but the
    # platform is inconsistent across chains - accept either.
    type: int | str | None = None
    status: int | str | None = None
    market_text: str | None = Field(None, alias="marketTextLong")
    promo_market_text: str | None = Field(None, alias="promoMarketTextLong")
    promo_price_per_unit: float | None = Field(None, alias="promoCalcPricePerUnit")
    promo_compare_price: float | None = Field(None, alias="promoComparePricePerUnit")
    comparable_price: float | None = Field(None, alias="comparablePrice")
    alternative_price: float | None = Field(None, alias="alternativePrice")
    trumf_campaign: bool | None = Field(None, alias="trumfCampaign")
    is_marketed: bool | None = Field(None, alias="isMarketed")
    weight_product: bool | None = Field(None, alias="weightProduct")
    valid_from: str | None = Field(None, alias="from")
    valid_to: str | None = Field(None, alias="to")
    tags: list[Any] = Field(default_factory=list)

    @property
    def is_price_reduction(self) -> bool:
        return self.promo_price_per_unit is not None


# ---------------------------------------------------------------------------
# Product
# ---------------------------------------------------------------------------
class Product(_Base):
    """A normalised Meny product.

    Populated from either a search hit or the full product endpoint. The full
    endpoint adds nutrition, allergens, origin and ``amountInterval``; search
    hits carry enough to rank and filter but NOT enough to build a cart line.
    """

    # --- identity
    ean: str
    title: str | None = None
    subtitle: str | None = None
    brand: str | None = None
    vendor: str | None = None
    slugified_url: str | None = Field(None, alias="slugifiedUrl")
    image_gtin: str | None = Field(None, alias="imageGtin")
    image_path: str | None = Field(None, alias="imagePath")

    # --- price
    price: float | None = None
    price_per_unit: float | None = Field(None, alias="pricePerUnit")
    price_per_unit_original: float | None = Field(None, alias="pricePerUnitOriginal")
    calc_price_per_unit: float | None = Field(None, alias="calcPricePerUnit")

    # --- comparable ("per measured unit") price - the key ranking signal
    compare_price_per_unit: float | None = Field(None, alias="comparePricePerUnit")
    compare_unit: str | None = Field(None, alias="compareUnit")
    comparable_unit_type: str | None = Field(None, alias="comparableUnitType")
    comparable_price: float | None = Field(None, alias="comparablePrice")

    # --- units & quantity rules
    unit: str | None = None
    unit_type: str | None = Field(None, alias="unitType")
    calc_unit: str | None = Field(None, alias="calcUnit")
    calc_unit_type: str | None = Field(None, alias="calcUnitType")
    unit_rule: float | None = Field(None, alias="unitRule")
    unit_rules: float | None = Field(None, alias="unitRules")
    amount_interval: float | None = Field(None, alias="amountInterval")
    weight: float | None = None
    measurement_value: float | None = Field(None, alias="measurementValue")
    measurement_type: str | None = Field(None, alias="measurementType")
    package_size: str | None = Field(None, alias="packageSize")

    # --- availability
    is_out_of_stock: bool = Field(False, alias="isOutOfStock")
    is_for_sale: bool = Field(True, alias="isForSale")
    is_revoked: bool = Field(False, alias="isRevoked")
    must_order_from_supplier: bool = Field(False, alias="mustOrderFromSupplier")
    unavailable_on_date: list[Any] = Field(default_factory=list,
                                           alias="unavailableOnDate")
    lead_time: list[Any] = Field(default_factory=list, alias="leadTime")
    # Present in the schema but observed null for every Meny product tested.
    # Treat as advisory only; never gate purchases on it.
    available_inventory: int | None = Field(None, alias="availableInventory")
    minimum_durability: int | None = Field(None, alias="minimumDurability")

    # --- classification
    #
    # Three levels, coarse to fine, and only the finest one answers "what IS
    # this". For a frozen cod fillet they read: categoryName "Fisk & skalldyr",
    # shoppingListGroupName "Fisk", shoppingListGroupName1 "Torsk". The first
    # two are shared with tinned cod roe, cod-flavoured cat food and a ready
    # meal in cod sauce; the third is not. See `leaf_group`.
    category_name: str | None = Field(None, alias="categoryName")
    category_id: str | int | None = Field(None, alias="categoryId")
    shopping_list_group_name: str | None = Field(None, alias="shoppingListGroupName")
    shopping_list_group_name1: str | None = Field(
        None, alias="shoppingListGroupName1")
    shopping_list_group_name2: str | None = Field(
        None, alias="shoppingListGroupName2")
    # Full-record-only restatement of the deepest populated level.
    shopping_list_group_name_leaf: str | None = Field(
        None, alias="shoppingListGroupNameLeaf")
    assortment_code: str | None = Field(None, alias="assortmentCode")

    # --- flags
    is_offer: bool = Field(False, alias="isOffer")
    uses_promotion_price: bool = Field(False, alias="usesPromotionPrice")
    is_bargain: bool = Field(False, alias="isBargain")
    is_fresh_produce: bool = Field(False, alias="isFreshProduce")
    is_local: bool = Field(False, alias="isLocal")
    is_new_product: bool = Field(False, alias="isNewProduct")
    is_medicin: bool = Field(False, alias="isMedicin")
    contains_alcohol: bool = Field(False, alias="containsAlcohol")
    alcohol_percentage: float | None = Field(None, alias="alcoholPercentage")
    age_limit: int = Field(0, alias="ageLimit")
    organic: bool | None = None
    product_sold_by_weight: bool = Field(False, alias="productSoldByWeight")
    product_by_weight_sold_as_item: bool = Field(
        False, alias="productByWeightSoldAsItem")
    is_bread_and_can_slice: bool = Field(False, alias="isBreadAndCanSlice")
    recycle_value: float = Field(0, alias="recycleValue")

    # --- rich detail (full endpoint only)
    description: str | None = None
    ingredients: str | None = None
    # Either free text or a temperature range dict such as
    # {"minTemperature": 0, "maxTemperature": 4}. Typing this as `str` used to
    # fail validation for every chilled product - which, because a fetch
    # failure is reported as "not found", silently dropped fresh goods from
    # carts. See `storage_text`.
    storage: Any = None
    allergens: Any = None
    allergens_list: Any = Field(None, alias="allergensList")
    nutritional_content: Any = Field(None, alias="nutritionalContent")
    country_of_origin: str | None = Field(None, alias="countryOfOrigin")
    synonyms: list[str] = Field(default_factory=list)
    environmental_codes: list[Any] = Field(default_factory=list,
                                           alias="environmentalCodes")
    promotions: list[Promotion] = Field(default_factory=list)

    # ---------------------------------------------------------------- derived
    @computed_field  # type: ignore[prop-decorator]
    @property
    def available(self) -> bool:
        """True when the product can actually be bought at this store."""
        return (
            self.is_for_sale
            and not self.is_out_of_stock
            and not self.is_revoked
            and not self.must_order_from_supplier
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def unit_group(self) -> str | None:
        """Canonical comparison group: 'mass' | 'volume' | 'count' | ...

        Two products may only be ranked on price-per-unit when their groups
        match. Comparing 245 kr/kg against 21 kr/l is meaningless.
        """
        unit = self.canonical_compare_unit
        return COMPARABLE_UNIT_GROUPS.get(unit) if unit else None

    @property
    def leaf_group(self) -> str | None:
        """The shop's most specific label for what this product is.

        The decisive matching signal, and free - it rides on every search hit.
        Word overlap cannot tell "Torskefilet" from "Torsk i Saus": both name
        the fish and both sit under group "Fisk". The shop separates them
        itself, one level down, as ``Torsk`` and ``Fiskemåltid ferdig``. Same
        for the whole avocado family: the fruit is ``Avocado``, the oil is
        ``Øvrig matolje``, the spread is ``Øvrig saus``.

        Deepest populated level wins; ``shoppingListGroupNameLeaf`` is the full
        record's own restatement of that and is preferred when present.
        """
        for candidate in (self.shopping_list_group_name_leaf,
                          self.shopping_list_group_name2,
                          self.shopping_list_group_name1):
            if candidate and candidate.strip():
                return candidate.strip()
        return None

    @property
    def freezer_goods(self) -> bool:
        """True when the shop files this under frozen goods.

        ``environmentalCodes`` carries ``frysevare`` outright, which is the
        only reliable way to know: "Torskefilet Blokk 400g" and "Torskefilet
        800g First Price" are both frozen and neither says so anywhere in its
        title, subtitle or group name.

        Deliberately reported separately from a form word in the *title*. A
        product marketed as "Brokkoli & Blomkål, frossen" is the frozen variant
        of a thing normally sold fresh; a product that merely lives in the
        freezer aisle is often the ordinary way that good is sold at all. The
        two deserve different penalties - see :func:`matching.judge`.
        """
        for entry in self.environmental_codes or []:
            name = (entry.get("name") if isinstance(entry, dict) else entry)
            if str(name or "").strip().lower() == "frysevare":
                return True
        return False

    @property
    def canonical_compare_unit(self) -> str | None:
        if self.comparable_unit_type:
            mapped = COMPARABLE_UNIT_MAP.get(self.comparable_unit_type)
            if mapped:
                return mapped
        if self.compare_unit:
            return self.compare_unit.strip().lower()
        return None

    @property
    def effective_price(self) -> float | None:
        """What you actually pay per item right now."""
        for candidate in (self.price_per_unit, self.calc_price_per_unit, self.price):
            if candidate is not None:
                return candidate
        return None

    @property
    def unit_price(self) -> float | None:
        """Price per comparable unit (kr/kg, kr/l, ...). The ranking key."""
        return self.compare_price_per_unit or self.comparable_price

    @property
    def on_sale(self) -> bool:
        return bool(
            self.is_offer
            or self.uses_promotion_price
            or any(p.is_price_reduction for p in self.promotions)
        )

    @property
    def min_quantity(self) -> float:
        """Smallest legal order quantity, in whatever unit the item is sold by.

        The trap here is that ``amountInterval`` means two different things
        depending on how the product is sold, and reading it unconditionally
        produces quantities no shop can fill.

        "Poteter / pr stk" is ordered as whole potatoes at 1.30 kr each
        (``unit: stk``, ``unitRule: 1``), but carries ``amountInterval: 0.13``
        and ``unitRules: 0.13`` - the WEIGHT of one potato, 130 g. Treating
        that as the order step yields 1.04 potatoes. Gul løk does the same with
        0.6. Nobody at the till will cut a potato into hundredths.

        So: only goods genuinely ordered BY WEIGHT (``productSoldByWeight``)
        step in fractions. Everything else - including
        ``productByWeightSoldAsItem`` produce, which is priced per kg but
        picked by the piece - steps in whole items.
        """
        if self.product_sold_by_weight:
            for candidate in (self.amount_interval, self.unit_rules,
                              self.unit_rule):
                if candidate:
                    return float(candidate)
            return 1.0
        step = self.unit_rule or self.unit_rules or 1.0
        return float(max(1, round(step)))

    @property
    def pack_quantity(self) -> float | None:
        """Physical size of one pack, expressed in the comparable unit.

        Distinct from ``min_quantity``, which is the ORDER STEP. A 2 kg bag of
        rice has ``unitRule=1`` (order one bag at a time) but a pack quantity
        of 2 kg. Confusing the two makes pack-size reasoning silently wrong.

        Preference order:
          1. ``measurementValue`` + ``measurementType`` (most precise)
          2. ``weight``, which tracks kg for mass and litres for volume
        """
        group = self.unit_group
        if self.measurement_value and self.measurement_type:
            value = float(self.measurement_value)
            unit = self.measurement_type.strip().lower()
            factors = {
                "g": 0.001, "gram": 0.001, "kg": 1.0,
                "ml": 0.001, "cl": 0.01, "dl": 0.1, "l": 1.0,
            }
            if unit in factors:
                converted = value * factors[unit]
                if group in ("mass", "volume"):
                    return round(converted, 4)
        if self.weight and group in ("mass", "volume"):
            return float(self.weight)
        if group == "count":
            return 1.0
        return None

    @property
    def pack_constraint(self) -> float | None:
        """Pack size when you must buy a whole pack, ``None`` when you need not.

        ``pack_quantity`` answers "how big is this", which is not the same
        question as "how little may I buy", and conflating them is expensive.
        The fresh counter's "Torsk Filet u/Skinn Vill pr Kg" is labelled
        ``packageSize: "2KG"`` with ``measurementValue: 2000`` - but it is
        ``productSoldByWeight`` with ``amountInterval: 0.2``, so 400 g is a
        legal order and the calculator prices it at 171.60 kr. Reading the 2 kg
        as a pack rounds a 400 g need up to one "pack" of 1 kg at 429 kr, and
        additionally makes the product look 5x too big for the need, so the
        pack-size guard throws it out of the ranking on top.

        For goods genuinely sold by weight there is no pack: the order step is
        ``min_quantity`` and anything above it is buyable.
        """
        if self.product_sold_by_weight:
            return None
        return self.pack_quantity

    @property
    def pieces_per_pack(self) -> float | None:
        """How many individual items one pack holds, when that is knowable.

        A recipe asks for "2 stk paprika". The shop sells paprika as a 2-pack,
        as a loose item, and in a 1 kg bag - and buying "2" of each of those is
        2, 2 and 2 *packs*, which is 4 peppers, 2 peppers and 2 kg. Without a
        piece count there is no way to turn a count need into a purchase.

        ``packageSize`` states it outright when it is a multi-pack ("2STK",
        "2pk"). Failing that, a bare "stykk"/"pr stk" marks a single loose
        item, as does ``productByWeightSoldAsItem`` - produce priced per kg but
        picked individually, where one order unit is one item.

        A bag or net ("1KG", "700G") returns None: its piece count is genuinely
        not in the data, and guessing it would invent quantities.

        The last rule below closes the commonest gap. Loose lemons, avocados
        and garlic reported ``pack_covers_count: unknown`` on a real week's
        planning even though the shop sells them one at a time - they simply
        carry no "stk" in any text field. But a product whose *comparable* unit
        is ``stk`` is priced per piece, and unless its pack size is stated as a
        mass or a volume, the thing being priced is the thing you pick up. One
        order unit, one item.
        """
        texts = [t for t in (self.package_size, self.subtitle, self.title) if t]
        for text in texts:
            match = _PIECES_RE.search(text)
            if match:
                count = int(match.group(1))
                if count > 0:
                    return float(count)
        if self.product_by_weight_sold_as_item:
            return 1.0
        if any(_SINGLE_RE.search(t) for t in texts):
            return 1.0
        if self.canonical_compare_unit == "stk" and not (
            self.package_size and _BULK_PACK_RE.match(self.package_size)
        ):
            return 1.0
        return None

    @property
    def price_per_piece(self) -> float | None:
        """Price of one individual item, for ranking against a count need."""
        pieces = self.pieces_per_pack
        price = self.effective_price
        if pieces and price is not None:
            return round(price / pieces, 4)
        return None

    def quantise(self, requested: float) -> float:
        """Round a requested quantity up to a legal multiple."""
        step = self.min_quantity
        if step <= 0:
            return max(1.0, requested)
        multiples = max(1, round(requested / step + 0.4999))
        value = multiples * step
        return round(value, 3)

    @property
    def storage_text(self) -> str | None:
        """Storage advice as a readable string, whichever shape it arrived in."""
        if isinstance(self.storage, dict):
            low = self.storage.get("minTemperature")
            high = self.storage.get("maxTemperature")
            if low is None and high is None:
                return None
            return f"{low}-{high} °C"
        return self.storage or None

    @property
    def url(self) -> str | None:
        if self.slugified_url:
            return f"https://meny.no/varer/{self.slugified_url.lstrip('/')}"
        return None

    def summary(self, *, verbose: bool = False) -> dict[str, Any]:
        """Compact dict for LLM consumption - full records are far too large.

        Keys whose value carries no information are dropped rather than sent as
        ``null``/``false``. A search returning 15 products is repeated context
        on every subsequent turn, so a field that says nothing is pure cost:
        omitting the empties roughly halves a typical result set.

        ``verbose`` adds the fields a human review needs (product URL, pack
        size, category) but a ranking pass does not.
        """
        data: dict[str, Any] = {"ean": self.ean, "title": self.title}
        # unit_group is derivable from unit; only worth sending when the caller
        # has to reason across groups.
        optional = {
            "price": self.effective_price,
            "unit_price": self.unit_price,
            "unit": self.canonical_compare_unit,
            "subtitle": self.subtitle,
            "brand": self.brand,
            "on_sale": self.on_sale or None,
            "organic": self.organic or None,
            "age_limit": self.age_limit or None,
            "unavailable": None if self.available else True,
            "min_quantity": self.min_quantity if self.min_quantity != 1.0 else None,
        }
        if verbose:
            optional.update({
                "unit_group": self.unit_group,
                "package_size": self.package_size,
                "category": self.shopping_list_group_name or self.category_name,
                "url": self.url,
            })
        data.update({k: v for k, v in optional.items() if v is not None})
        return data


# ---------------------------------------------------------------------------
# Cart
# ---------------------------------------------------------------------------
class CartLine(_Base):
    """One line of the cart, in the exact shape the platform expects.

    Verified by bisection against ``PUT /api/calculator``: omitting ``product``
    yields ``400 Invalid payload``. The full product object must be embedded.
    """

    id: str
    ean: str
    quantity: float = 1
    added_time: str = Field(alias="addedTime")
    has_been_synced: bool = Field(False, alias="hasBeenSynced")
    rev: int = Field(1, alias="__rev__")
    removed: bool = Field(False, alias="__removed__")
    type: Literal["CART_PRODUCT"] = Field("CART_PRODUCT", alias="__type__")
    price_per_unit: float = Field(0, alias="pricePerUnit")
    line_price: float = Field(0, alias="linePrice")
    compare_price_per_unit: float = Field(0, alias="comparePricePerUnit")
    total_discount: float = Field(0, alias="totalDiscount")
    product: dict[str, Any]
    does_not_exist_in_current_store: bool = Field(
        False, alias="doesNotExistInCurrentStore")
    substitute: Any = None
    should_slice: bool = Field(False, alias="shouldSlice")

    @classmethod
    def build(cls, product: Product, raw: dict[str, Any],
              quantity: float = 1) -> "CartLine":
        qty = product.quantise(quantity)
        ppu = product.effective_price or 0.0
        return cls(
            id=product.ean,
            ean=product.ean,
            quantity=qty,
            addedTime=datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
            pricePerUnit=ppu,
            linePrice=round(ppu * qty, 2),
            comparePricePerUnit=product.compare_price_per_unit or ppu,
            totalDiscount=0,
            product=raw,
        )

    def to_payload(self) -> dict[str, Any]:
        """Serialise using the platform's own key names."""
        return self.model_dump(by_alias=True, exclude_none=False)


class CartTotals(_Base):
    """Totals from the calculator.

    Semantics verified against a live 3-line cart (chicken 3x49.90, kefir
    1x20.00, milk 2x21.40):

        calculatorTotalFromMcpe  212.50  raw sum of line prices
        totalOfferDiscount        14.50  offers already reflected in line prices
        calculatorTotal          227.00  = raw sum + offer discount (list price)
        totalFees                  6.75  bag / handling fees
        buffer                    26.50  reserve for weight-variable goods
        totalToPay               245.75  what the customer is actually charged

    The key consequence: ``totalToPay`` is materially higher than the sum of
    item prices, because of fees and the fresh-goods buffer. Never present a
    locally-summed total as the price - always use the calculator.
    """

    # Raw sum of line prices before fees/buffer ("MCPE" = the pricing engine).
    calculator_total_from_engine: float | None = Field(
        None, alias="calculatorTotalFromMcpe")
    calculator_total: float | None = Field(None, alias="calculatorTotal")
    total_to_pay: float | None = Field(None, alias="totalToPay")
    total_discount: float | None = Field(None, alias="totalDiscount")
    total_recycle_value: float | None = Field(None, alias="totalRecycleValue")
    total_weight: float | None = Field(None, alias="totalWeight")
    number_of_items: float | None = Field(None, alias="numberOfItemsOrdered")
    total_fees: float | None = Field(None, alias="totalFees")
    total_offer_discount: float | None = Field(None, alias="totalOfferDiscount")
    # Reserve held for items sold by weight, where the picked weight varies.
    buffer: float | None = None


class CalculatedCart(_Base):
    """Response of ``PUT /api/calculator/{chain}/{store}``."""

    cart: list[dict[str, Any]] = Field(default_factory=list)
    discounts: list[dict[str, Any]] = Field(default_factory=list)
    totals: CartTotals = Field(default_factory=CartTotals)
    fees: list[dict[str, Any]] = Field(default_factory=list)
    # Mix-and-match offers the cart is short of - free upsell hints.
    missing_mix_match: list[dict[str, Any]] = Field(
        default_factory=list, alias="missingMixMatch")

    @property
    def unavailable_eans(self) -> list[str]:
        return [
            line.get("ean", "")
            for line in self.cart
            if line.get("doesNotExistInCurrentStore")
        ]
