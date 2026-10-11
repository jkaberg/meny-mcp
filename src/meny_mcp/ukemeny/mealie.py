"""Async Mealie REST client.

Only the endpoints the weekly run needs. Shares the server's httpx dependency
rather than reaching for a second HTTP stack.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..config import settings


class MealieError(RuntimeError):
    pass


class Mealie:
    def __init__(self, url: str | None = None, token: str | None = None,
                 timeout: float = 30.0) -> None:
        self.url = (url or settings.mealie_url).rstrip("/")
        self.token = token or settings.mealie_token
        self.timeout = timeout
        if not self.url or not self.token:
            raise MealieError(
                "Mealie is not configured on this server. Set MEALIE_URL and "
                "MEALIE_TOKEN in the server's environment (in Claude Desktop: "
                "the env block of this server's entry in "
                "claude_desktop_config.json) and restart it."
            )

    async def _request(self, method: str, path: str,
                       params: dict[str, Any] | None = None,
                       body: Any = None) -> Any:
        headers = {"Authorization": f"Bearer {self.token}",
                   "Accept": "application/json"}
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as http:
                resp = await http.request(method, f"{self.url}/api{path}",
                                          params=clean or None, json=body,
                                          headers=headers)
        except httpx.HTTPError as exc:
            raise MealieError(f"{method} {path}: {exc}") from None
        if resp.status_code >= 400:
            detail = resp.text[:300]
            if "already exists" in detail.lower() and method in ("PUT", "PATCH"):
                detail += (
                    "  [this is Mealie's generic message for any SQL integrity "
                    "error on a recipe write, not a duplicate-name check - "
                    "most often a required id written as NULL]")
            raise MealieError(
                f"{method} {path} -> HTTP {resp.status_code}: {detail}")
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    async def paged(self, path: str, per_page: int = 100,
                    **params: Any) -> list[dict]:
        """Walk every page.

        Mealie caps perPage server-side on some deployments, so asking for 500
        and assuming one page silently truncates the food list - which then
        looks like a missing food and gets created as a duplicate.
        """
        out: list[dict] = []
        page = 1
        while True:
            payload = await self._request("GET", path,
                                          {"page": page, "perPage": per_page,
                                           **params})
            items = payload.get("items", []) if isinstance(payload, dict) else []
            out.extend(items)
            if page >= ((payload or {}).get("total_pages") or 1) or not items:
                break
            page += 1
        return out

    async def recipes(self) -> list[dict]:
        return await self.paged("/recipes", orderBy="created_at",
                                orderDirection="desc")

    async def recipe(self, slug: str) -> dict:
        return await self._request("GET", f"/recipes/{slug}")

    async def create_recipe(self, name: str) -> str:
        out = await self._request("POST", "/recipes", body={"name": name})
        return out if isinstance(out, str) else (out or {}).get("slug", "")

    async def update_recipe(self, slug: str, body: dict) -> Any:
        """Update a recipe with PATCH, not PUT.

        PUT here is a trap. Mealie's update applies the submitted object field
        by field, so a PUT that omits ``id``/``userId``/``householdId``/
        ``groupId`` writes NULL over them - which fails as a SQL integrity
        error, and Mealie's recipe controller reports *every* integrity error
        with the same message: **"Recipe already exists"**. That message names
        neither the real cause nor the real field, and it is returned even when
        updating a recipe that plainly does exist and is not being renamed.

        Verified against Mealie 3.22 on 2026-08-20:

        =========================================  ======================
        Request                                    Result
        =========================================  ======================
        ``PUT`` without ``id``                     400 "Recipe already exists"
        ``PUT`` with id/userId/householdId/groupId 200
        ``PATCH`` without any of them              200
        =========================================  ======================

        PATCH also leaves unsent fields alone, so ``dateAdded``, ``createdAt``,
        ``image``, ``comments`` and the rest survive a write instead of being
        nulled by omission - which the PUT path did silently even when it
        succeeded.
        """
        return await self._request("PATCH", f"/recipes/{slug}", body=body)

    async def units(self) -> list[dict]:
        return await self.paged("/units")

    async def foods(self) -> list[dict]:
        return await self.paged("/foods")

    async def tags(self) -> list[dict]:
        return await self.paged("/organizers/tags")

    async def create_unit(self, name: str) -> dict:
        return await self._request("POST", "/units",
                                   body={"name": name, "abbreviation": name,
                                         "useAbbreviation": True})

    async def create_food(self, name: str) -> dict:
        return await self._request("POST", "/foods", body={"name": name})

    async def mealplans(self, start: str, end: str) -> list[dict]:
        return await self.paged("/households/mealplans",
                                start_date=start, end_date=end)

    async def create_mealplan(self, date: str, recipe_id: str,
                              entry_type: str = "dinner") -> dict:
        return await self._request("POST", "/households/mealplans",
                                   body={"date": date, "entryType": entry_type,
                                         "recipeId": recipe_id})
