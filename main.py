import os
import secrets
from typing import List, Optional, Dict, Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, EmailStr
import requests

from database import db, create_document, get_documents

app = FastAPI(title="SaaS Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------
# Environment config
# -----------------
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "http://localhost:8000/auth/discord/callback")
REVOLUT_API_KEY = os.getenv("REVOLUT_API_KEY")
REVOLUT_API_BASE = os.getenv("REVOLUT_API_BASE", "https://merchant.revolut.com/api")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")


# -----------------
# Models
# -----------------
class ContactMessage(BaseModel):
    name: str = Field(..., min_length=1)
    email: EmailStr
    message: str = Field(..., min_length=5, max_length=2000)


class AnalyticsEvent(BaseModel):
    name: str
    user_id: Optional[str] = None
    properties: Dict[str, Any] = Field(default_factory=dict)


class CheckoutItem(BaseModel):
    product_id: str
    quantity: int = Field(1, ge=1)


class CheckoutRequest(BaseModel):
    items: List[CheckoutItem]
    customer_email: Optional[EmailStr] = None
    plan: Optional[str] = Field(None, description="Bronze | Silver | Gold | Platinum")


# -----------------
# Utilities
# -----------------
# Demo product catalog (these are also returned by /api/products)
PRODUCTS = [
    {
        "id": "bronze",
        "name": "Bronze",
        "price": 900,  # in minor units (e.g., cents)
        "currency": "USD",
        "features": ["Starter limits", "Email support"],
        "mostPopular": False,
    },
    {
        "id": "silver",
        "name": "Silver",
        "price": 1900,
        "currency": "USD",
        "features": ["Increased quotas", "Priority support"],
        "mostPopular": True,
    },
    {
        "id": "gold",
        "name": "Gold",
        "price": 4900,
        "currency": "USD",
        "features": ["High limits", "Priority + SLA"],
        "mostPopular": False,
    },
    {
        "id": "platinum",
        "name": "Platinum",
        "price": 9900,
        "currency": "USD",
        "features": ["Max limits", "Dedicated support"],
        "mostPopular": False,
    },
]


def get_product(pid: str):
    return next((p for p in PRODUCTS if p["id"] == pid), None)


# -----------------
# Basic routes
# -----------------
@app.get("/")
def root():
    return {"status": "ok", "service": "backend"}


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set",
        "database_name": "✅ Set" if os.getenv("DATABASE_NAME") else "❌ Not Set",
        "connection_status": "Not Connected",
        "collections": [],
    }
    try:
        if db is not None:
            response["database"] = "✅ Available"
            try:
                response["collections"] = db.list_collection_names()[:10]
                response["database"] = "✅ Connected & Working"
                response["connection_status"] = "Connected"
            except Exception as e:
                response["database"] = f"⚠️ Connected but Error: {str(e)[:80]}"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:80]}"
    return response


# -----------------
# Products & Pricing
# -----------------
@app.get("/api/products")
def list_products():
    return {"products": PRODUCTS}


# -----------------
# Contact form
# -----------------
@app.post("/api/contact")
def submit_contact(payload: ContactMessage):
    doc_id = create_document("contactmessage", payload)
    return {"ok": True, "id": doc_id}


# -----------------
# Analytics events
# -----------------
@app.post("/api/analytics")
def capture_event(event: AnalyticsEvent, request: Request):
    data = event.model_dump()
    data["ip"] = request.client.host if request.client else None
    data["user_agent"] = request.headers.get("user-agent")
    doc_id = create_document("analyticsevent", data)
    return {"ok": True, "id": doc_id}


# -----------------
# Discord OAuth (Login with Discord)
# -----------------
@app.get("/auth/discord/login")
def discord_login():
    if not DISCORD_CLIENT_ID:
        # In development, if not configured, just mock login
        return {"mock": True, "login_url": f"{FRONTEND_URL}/auth/success?provider=discord"}
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": DISCORD_REDIRECT_URI,
        "scope": "identify email",
        "state": state,
        "prompt": "consent",
    }
    query = "&".join([f"{k}={requests.utils.quote(str(v))}" for k, v in params.items()])
    return {"login_url": f"https://discord.com/api/oauth2/authorize?{query}"}


@app.get("/auth/discord/callback")
def discord_callback(code: Optional[str] = None, state: Optional[str] = None):
    if not code:
        raise HTTPException(status_code=400, detail="Missing code")
    if not DISCORD_CLIENT_ID or not DISCORD_CLIENT_SECRET:
        # Mock success if secrets not provided
        return RedirectResponse(url=f"{FRONTEND_URL}/auth/success?provider=discord&mock=true")

    # Exchange code for token
    token_url = "https://discord.com/api/oauth2/token"
    data = {
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": DISCORD_REDIRECT_URI,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    resp = requests.post(token_url, data=data, headers=headers)
    if resp.status_code != 200:
        raise HTTPException(status_code=400, detail="Discord token exchange failed")
    token = resp.json().get("access_token")

    # Fetch user info
    u = requests.get("https://discord.com/api/users/@me", headers={"Authorization": f"Bearer {token}"})
    profile = u.json() if u.status_code == 200 else {}

    # Store/track login event
    create_document("loginevent", {"provider": "discord", "profile": profile})

    return RedirectResponse(url=f"{FRONTEND_URL}/auth/success?provider=discord")


# -----------------
# Checkout with Revolut Business
# -----------------
@app.post("/api/checkout")
def create_checkout_session(payload: CheckoutRequest):
    if not payload.items and not payload.plan:
        raise HTTPException(status_code=400, detail="No items provided")

    # Build line items
    line_items = []
    total = 0
    items = payload.items or []
    if payload.plan and not items:
        # Single plan purchase maps to product id
        prod = get_product(payload.plan.lower())
        if not prod:
            raise HTTPException(status_code=404, detail="Plan not found")
        items = [CheckoutItem(product_id=prod["id"], quantity=1)]

    for it in items:
        p = get_product(it.product_id)
        if not p:
            raise HTTPException(status_code=404, detail=f"Product not found: {it.product_id}")
        amount = p["price"] * it.quantity
        total += amount
        line_items.append({
            "name": p["name"],
            "quantity": it.quantity,
            "amount": p["price"],
            "currency": p["currency"],
        })

    # Create order record regardless of payment provider result
    order_id = create_document("order", {
        "items": [li.copy() for li in line_items],
        "total": total,
        "currency": line_items[0]["currency"] if line_items else "USD",
        "provider": "revolut",
        "status": "created",
        "customer_email": payload.customer_email,
    })

    # If Revolut is configured, attempt to create a payment link
    if REVOLUT_API_KEY:
        try:
            # Revolut Merchant API: create order link
            url = f"{REVOLUT_API_BASE}/1.0/orders"
            headers = {
                "Authorization": f"Bearer {REVOLUT_API_KEY}",
                "Content-Type": "application/json",
            }
            body = {
                "amount": total,
                "currency": line_items[0]["currency"] if line_items else "USD",
                "capture_mode": "AUTOMATIC",
                "merchant_order_id": order_id,
                "description": "SaaS plan purchase",
                "customer": {"email": payload.customer_email} if payload.customer_email else None,
            }
            # remove None values
            body = {k: v for k, v in body.items() if v is not None}
            r = requests.post(url, json=body, headers=headers, timeout=15)
            if r.status_code in (200, 201):
                data = r.json()
                payment_id = data.get("id") or data.get("public_id")
                link = data.get("public_url") or data.get("checkout_url") or f"{FRONTEND_URL}/thank-you?order={order_id}"
                create_document("paymentproviderlog", {"provider": "revolut", "request": body, "response": data})
                return {"ok": True, "order_id": order_id, "payment_id": payment_id, "payment_url": link}
            else:
                create_document("paymentproviderlog", {"provider": "revolut", "request": body, "status": r.status_code, "error": r.text})
        except Exception as e:
            create_document("paymentproviderlog", {"provider": "revolut", "error": str(e)})

    # Fallback mock link if Revolut not configured or failed
    return {"ok": True, "order_id": order_id, "payment_url": f"{FRONTEND_URL}/thank-you?order={order_id}&mock=true"}


# Health for frontend to detect
@app.get("/api/health")
def health():
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
