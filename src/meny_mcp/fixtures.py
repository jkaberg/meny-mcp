"""Fixtures captured from live meny.no responses on 2026-08-15.

Trimmed to the fields the models care about; shapes and value types are
verbatim from the API.
"""

# A real search hit: promoted organic kefir, reduced 34.50 -> 20.00
KEFIR_SOURCE = {
    "ean": "7038010054822",
    "title": "Kefir",
    "subtitle": "Økologisk 1000g Tine",
    "brand": "Tine",
    "vendor": "Tine sa",
    "slugifiedUrl": "/meieri-egg/melk/kefir/kefir-7038010054822",
    "imageGtin": "7038010054822",
    "pricePerUnit": 20,
    "pricePerUnitOriginal": 34.5,
    "calcPricePerUnit": 20,
    "comparePricePerUnit": 20,
    "compareUnit": "kg",
    "unit": "stk",
    "unitType": "Stykk",
    "calcUnit": "stk",
    "calcUnitType": "Stykk",
    "unitRule": 1,
    "unitRules": 1,
    "weight": 1,
    "isOutOfStock": False,
    "isForSale": True,
    "isRevoked": False,
    "mustOrderFromSupplier": False,
    "availableInventory": None,
    "categoryName": "Meieri & egg",
    "shoppingListGroupName": "Melk",
    "isOffer": True,
    "usesPromotionPrice": True,
    "isFreshProduce": False,
    "containsAlcohol": False,
    "ageLimit": 0,
    "productSoldByWeight": False,
    "recycleValue": 0,
    "promotions": [
        {
            "promotionId": 152189,
            "name": "MENY KM KJØL KR 20 UKE 33-34",
            "type": 2,
            "status": 1,
            "promoCalcPricePerUnit": 20,
            "comparablePrice": 20,
            "from": "2026-08-09T00:00:00+02:00",
            "to": "2026-08-22T23:59:59+02:00",
            "tags": [],
        }
    ],
}

# Full product endpoint response: chicken fillet, sold per pack but compared /kg
KYLLINGFILET_PRODUCT = {
    "ean": "7039610007911",
    "title": "Kyllingfilet",
    "subtitle": "ca 210g Norsk Kylling",
    "brand": "Norsk Kylling",
    "price": 49.9,
    "pricePerUnit": 49.9,
    "pricePerUnitOriginal": 49.9,
    "calcPricePerUnit": 49.9,
    "comparePricePerUnit": 237.62,
    "comparableUnitType": "Kilo",
    "compareUnit": "kg",
    "comparablePrice": 237.62,
    "measurementValue": 210,
    "measurementType": "g",
    "unit": "stk",
    "unitType": "Stykk",
    "calcUnit": "stk",
    "calcUnitType": "Stykk",
    "unitRule": 1,
    "unitRules": 1,
    "amountInterval": 1,
    "weight": 0.21,
    "packageSize": "ca 210g",
    "isOutOfStock": False,
    "isForSale": True,
    "isRevoked": False,
    "mustOrderFromSupplier": False,
    "unavailableOnDate": [],
    "leadTime": [],
    "assortmentCode": "41",
    "categoryName": "Kylling og fjærkre",
    "shoppingListGroupName": "Kylling",
    "isOffer": False,
    "usesPromotionPrice": False,
    "isFreshProduce": True,
    "containsAlcohol": False,
    "ageLimit": 0,
    "organic": False,
    "recycleValue": 0,
    "countryOfOrigin": "Norge",
    "promotions": [],
}

# Weight goods: minimum order is 0.5 kg in 0.1 kg steps
LOOSE_MINCE_PRODUCT = {
    "ean": "2000000000015",
    "title": "Kjøttdeig av Storfe",
    "subtitle": "Ferskvaredisk",
    "pricePerUnit": 189.0,
    "comparePricePerUnit": 189.0,
    "comparableUnitType": "Kilo",
    "compareUnit": "kg",
    "unit": "kg",
    "unitType": "Kilo",
    "unitRule": 0.1,
    "unitRules": 0.1,
    "amountInterval": 0.5,
    "productSoldByWeight": True,
    "isOutOfStock": False,
    "isForSale": True,
    "isRevoked": False,
}

# Unavailable: withdrawn from sale
REVOKED_PRODUCT = {
    "ean": "7038010041914",
    "title": "Kremfløte 37%",
    "pricePerUnit": 29.5,
    "comparePricePerUnit": 98.33,
    "compareUnit": "l",
    "comparableUnitType": "Liter",
    "isOutOfStock": True,
    "isForSale": True,
    "isRevoked": False,
}

SEARCH_PAYLOAD = {
    "products": {
        "hits": [
            {"contentData": {"_source": KEFIR_SOURCE}},
            {"contentData": {"_source": KYLLINGFILET_PRODUCT}},
            {"contentData": {"_source": REVOKED_PRODUCT}},
            {"contentData": {"_source": {"title": "no ean - must be skipped"}}},
        ]
    }
}

# PUT /api/calculator response envelope
CALCULATOR_RESPONSE = {
    "cart": [
        {"ean": "7039610007911", "quantity": 3, "pricePerUnit": 49.9,
         "linePrice": 149.7, "doesNotExistInCurrentStore": False},
        {"ean": "7038010054822", "quantity": 1, "pricePerUnit": 20.0,
         "linePrice": 20.0, "doesNotExistInCurrentStore": True},
    ],
    "discounts": [],
    "totals": {
        "calculatorTotal": 169.7,
        "totalToPay": 169.7,
        "totalDiscount": 14.5,
        "totalRecycleValue": 0,
        "totalWeight": 1.63,
        "numberOfItemsOrdered": 4,
        "totalFees": 0,
        "totalOfferDiscount": 14.5,
    },
    "fees": [],
    "missingMixMatch": [],
}
