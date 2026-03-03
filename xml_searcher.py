from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
import httpx
import xml.etree.ElementTree as ET
import asyncio
from typing import List, Optional, Dict, Any
from pydantic import BaseModel
import re
import html
import json
from difflib import SequenceMatcher
app = FastAPI(title="Septic Store API", description="Microservice for searching products in XML feed")
# Allow CORS from any origin (for Make.com and widget)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
class SearchRequest(BaseModel):
    q: Optional[str] = None
    category_id: Optional[str] = None
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    users: Optional[str] = None
    limit: int = 10
# Global cache
CACHE = {
    "categories": {}, # id -> {"id": str, "name": str, "parent_id": str}
    "products": [],   # list of product dicts
    "last_updated": None
}
FEED_URL = "https://lenkanal.ru/bitrix/catalog_export/fid.xml"
# Latin to Cyrillic transliteration map
LATIN_TO_CYRILLIC = {
    'a': 'а', 'b': 'б', 'v': 'в', 'g': 'г', 'd': 'д', 'e': 'е',
    'zh': 'ж', 'z': 'з', 'i': 'и', 'y': 'й', 'k': 'к', 'l': 'л',
    'm': 'м', 'n': 'н', 'o': 'о', 'p': 'п', 'r': 'р', 's': 'с',
    't': 'т', 'u': 'у', 'f': 'ф', 'kh': 'х', 'h': 'х', 'ts': 'ц',
    'ch': 'ч', 'sh': 'ш', 'shch': 'щ', 'yu': 'ю', 'ya': 'я',
}
# Common brand typos/misspellings -> correct name
TYPO_CORRECTIONS = {
    'топаз': 'топас', 'topas': 'топас', 'topaz': 'топас',
    'евролас': 'евролос', 'евралос': 'евролос', 'evrolos': 'евролос',
    'биодэка': 'биодека', 'biodeka': 'биодека',
    'юнилос': 'юнилос астра', 'unilos': 'юнилос астра',
    'астра': 'юнилос астра',
    'генезис': 'генезис', 'genesis': 'генезис',
    'коловеси': 'коло веси', 'kolo vesi': 'коло веси',
    'термит': 'термит', 'termit': 'термит',
    'тритон': 'тритон', 'triton': 'тритон',
    'гарда': 'garda', 'garda': 'garda',
    'аквалос': 'аквалос', 'akvalos': 'аквалос',
    'альтабио': 'альта био', 'alta bio': 'альта био',
    'биотанк': 'биотанк', 'biotank': 'биотанк',
    'росток': 'росток', 'rostok': 'росток',
    'танк': 'танк', 'tank': 'танк',
    'лидер': 'лидер', 'lider': 'лидер',
    'кессон': 'кессон', 'kesson': 'кессон',
}
def transliterate_to_cyrillic(text: str) -> str:
    """Convert Latin text to Cyrillic approximation."""
    result = text.lower()
    # Sort by length (longer first) to handle 'shch' before 'sh'
    for lat, cyr in sorted(LATIN_TO_CYRILLIC.items(), key=lambda x: -len(x[0])):
        result = result.replace(lat, cyr)
    return result
def fix_typos(query: str) -> str:
    """Fix common typos and transliterate if needed."""
    q_lower = query.lower().strip()
    # Check direct typo match
    for typo, correct in TYPO_CORRECTIONS.items():
        if typo in q_lower:
            q_lower = q_lower.replace(typo, correct)
            return q_lower
    # If query has Latin characters, try transliteration
    if re.search(r'[a-zA-Z]', query):
        return transliterate_to_cyrillic(q_lower)
    return q_lower
def fuzzy_match(word: str, target: str, threshold: float = 0.7) -> float:
    """Return similarity ratio between word and target."""
    return SequenceMatcher(None, word.lower(), target.lower()).ratio()
def clean_text(text: str) -> str:
    if not text:
        return ""
    # Remove CDATA wrapper if present
    text = text.replace("<![CDATA[", "").replace("]]>", "")
    # Decode html entities
    text = html.unescape(text)
    # Remove HTML tags
    text = re.sub(r'<[^>]+>', ' ', text)
    # Clean up whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text
async def fetch_and_parse_xml():
    print("Fetching XML...")
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(FEED_URL, timeout=30.0)
            response.raise_for_status()
            
            root = ET.fromstring(response.content)
            
            categories = {}
            for category in root.findall(".//category"):
                cat_id = category.get("id")
                parent_id = category.get("parentId")
                name = category.text
                categories[cat_id] = {
                    "id": cat_id,
                    "name": name,
                    "parent_id": parent_id
                }
                
            products = []
            for offer in root.findall(".//offer"):
                if offer.get("available") != "true":
                    continue
                    
                product = {
                    "id": offer.get("id"),
                    "name": offer.findtext("name", ""),
                    "price": float(offer.findtext("price", "0")),
                    "url": offer.findtext("url", ""),
                    "category_id": offer.findtext("categoryId", ""),
                    "description": clean_text(offer.findtext("description", "")),
                    "params": {}
                }
                
                # Extract params (brand, size, etc.)
                for param in offer.findall("param"):
                    param_name = param.get("name")
                    param_value = param.text
                    if param_name and param_value:
                        product["params"][param_name] = param_value
                        
                # Add category name for easier search
                cat_id = product["category_id"]
                if cat_id in categories:
                    product["category_name"] = categories[cat_id]["name"]
                    
                products.append(product)
                
            CACHE["categories"] = categories
            CACHE["products"] = products
            CACHE["last_updated"] = asyncio.get_event_loop().time()
            print(f"Loaded {len(categories)} categories and {len(products)} products.")
            
        except Exception as e:
            print(f"Error fetching/parsing XML: {e}")
@app.on_event("startup")
async def startup_event():
    # Fetch immediately on startup
    await fetch_and_parse_xml()
    # TODO: Add background task for periodic updates if needed
@app.get("/categories")
async def get_categories():
    """Returns all categories."""
    return {"categories": list(CACHE["categories"].values())}
def _score_product(p, q_words, q_original):
    """Score a single product against query words. Returns score or 0."""
    score = 0
    name_lower = p["name"].lower()
    brand = p["params"].get("Бренд", "").lower()
    category = p.get("category_name", "").lower()
    all_params = " ".join(p["params"].values()).lower()
    
    for word in q_words:
        if word in name_lower:
            score += 10
        elif word in brand:
            score += 8
        elif word in category:
            score += 5
        elif word in all_params:
            score += 3
        elif word in p["description"].lower():
            score += 1
        else:
            # Fuzzy match against brand name
            if brand and fuzzy_match(word, brand) >= 0.75:
                score += 6
            # Fuzzy match against words in product name
            elif any(fuzzy_match(word, nw) >= 0.75 for nw in name_lower.split() if len(nw) > 2):
                score += 4
    
    if score == 0:
        return 0
    
    # Bonus for exact full query match in name
    if q_original and q_original.lower() in name_lower:
        score += 20
    
    return score
def _do_search(q=None, category_id=None, min_price=None, max_price=None, users=None, limit=10):
    """Core search logic with fuzzy matching and typo correction."""
    original_query = q
    
    # Step 1: Fix typos and transliterate
    if q:
        q_fixed = fix_typos(q)
    else:
        q_fixed = q
    
    # Step 2: Try search with corrected query
    scored_results = []
    q_words = [w.lower() for w in q_fixed.split()] if q_fixed else []
    
    for p in CACHE["products"]:
        if category_id and p["category_id"] != category_id:
            continue
        if min_price is not None and p["price"] < min_price:
            continue
        if max_price is not None and p["price"] > max_price:
            continue
        if users and p["params"].get("Количество пользователей", "") != users:
            continue
        
        if q_words:
            score = _score_product(p, q_words, q_fixed)
            if score > 0:
                scored_results.append((score, p))
        else:
            scored_results.append((0, p))
    
    # Step 3: If no results and query was corrected, note that
    corrected = q_fixed != (q.lower().strip() if q else q)
    
    scored_results.sort(key=lambda x: (-x[0], x[1]["price"]))
    results = [item[1] for item in scored_results[:limit]]
    
    response = {"results": results, "total_found": len(results), "query": original_query}
    if corrected and results:
        response["corrected_query"] = q_fixed
        response["note"] = f"Исправлено: '{original_query}' → '{q_fixed}'"
    
    return response
@app.get("/search")
async def search_products(
    q: Optional[str] = Query(None),
    category_id: Optional[str] = Query(None),
    min_price: Optional[float] = Query(None),
    max_price: Optional[float] = Query(None),
    users: Optional[str] = Query(None),
    limit: int = Query(10)
):
    return _do_search(q=q, category_id=category_id, min_price=min_price,
                      max_price=max_price, users=users, limit=limit)
@app.post("/search")
async def search_products_post(body: SearchRequest):
    return _do_search(q=body.q, category_id=body.category_id,
                      min_price=body.min_price, max_price=body.max_price,
                      users=body.users, limit=body.limit)
@app.get("/find/{query}")
async def find_products(query: str, limit: int = 10):
    """Search by query in URL path. Example: /find/Топас 5"""
    return _do_search(q=query, limit=limit)
@app.post("/")
async def catch_all_post(request: Request):
    """Catch-all POST for AI Agent flexibility."""
    try:
        data = await request.json()
        q = data.get("q") or data.get("query") or data.get("search") or data.get("queryParameters", {}).get("q")
        limit = data.get("limit", 10)
        if not q:
            for v in data.values():
                if isinstance(v, str) and len(v) > 1:
                    q = v
                    break
                elif isinstance(v, dict):
                    for vv in v.values():
                        if isinstance(vv, str) and len(vv) > 1:
                            q = vv
                            break
        return _do_search(q=q, limit=limit)
    except Exception as e:
        return {"error": str(e), "results": [], "total_found": 0}
@app.get("/product/{product_id}")
async def get_product(product_id: str):
    """Get a specific product by ID."""
    for p in CACHE["products"]:
        if p["id"] == product_id:
            return p
    raise HTTPException(status_code=404, detail="Product not found")
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
