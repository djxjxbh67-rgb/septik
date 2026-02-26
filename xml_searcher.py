from fastapi import FastAPI, HTTPException, Query
import httpx
import xml.etree.ElementTree as ET
import asyncio
from typing import List, Optional, Dict, Any
from pydantic import BaseModel
import re
import html

app = FastAPI(title="Septic Store API", description="Microservice for searching products in XML feed")

# Global cache
CACHE = {
    "categories": {}, # id -> {"id": str, "name": str, "parent_id": str}
    "products": [],   # list of product dicts
    "last_updated": None
}

FEED_URL = "https://lenkanal.ru/bitrix/catalog_export/fid.xml"

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

@app.get("/search")
async def search_products(
    q: Optional[str] = Query(None, description="Search query in name, description or brand"),
    category_id: Optional[str] = Query(None, description="Filter by category ID"),
    limit: int = Query(10, description="Max results to return")
):
    """Search for products."""
    results = []
    
    q_lower = q.lower() if q else None
    
    for p in CACHE["products"]:
        # Filter by category
        if category_id and p["category_id"] != category_id:
            continue
            
        # Filter by query
        if q_lower:
            match = False
            # Check name
            if q_lower in p["name"].lower():
                match = True
            # Check brand
            elif "Бренд" in p["params"] and q_lower in p["params"]["Бренд"].lower():
                match = True
            # Check description (basic)
            elif q_lower in p["description"].lower():
                match = True
                
            if not match:
                continue
                
        results.append(p)
        if len(results) >= limit:
            break
            
    return {"results": results, "total_found": len(results)}

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
