#!/usr/bin/env python3
# app.py
import os
import json
import time
import uuid
import logging
import threading
from logging.handlers import RotatingFileHandler
from flask import Flask, request, jsonify, make_response

app = Flask(__name__)

# ----------------------------
# Configuration
# ----------------------------
LOG_DIR = os.environ.get("LOG_DIR", "logs")
LOG_FILE = os.path.join(LOG_DIR, "shop.log")
PORT = int(os.environ.get("PORT", "8080"))
HOST = os.environ.get("HOST", "0.0.0.0")
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"

os.makedirs(LOG_DIR, exist_ok=True)

# ----------------------------
# Logging
# ----------------------------
logger = logging.getLogger("shop")
logger.setLevel(logging.INFO)

# Rotating file handler: ~5MB per file, keep 3 backups
handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3)
# Keep the log message as the JSON payload to be easy to parse (one JSON per line)
formatter = logging.Formatter("%(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)

def log_event(event_type: str, **fields):
    """Write a single JSON event line to the log file."""
    event = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event_type,
        "remote_addr": request.headers.get("X-Forwarded-For", request.remote_addr),
        "method": request.method,
        "path": request.path,
        "request_id": request.headers.get("X-Request-Id", str(uuid.uuid4())),
        "session_id": get_or_create_session_id(from_request_only=True),  # don't set header here
    }
    event.update(fields)
    logger.info(json.dumps(event, ensure_ascii=False))

# ----------------------------
# Domain Model (in-memory)
# ----------------------------
catalog = {
    "1": {"id": "1", "name": "Mechanical Keyboard", "price": 99.90},
    "2": {"id": "2", "name": "Wireless Mouse", "price": 39.50},
    "3": {"id": "3", "name": "USB-C Hub", "price": 29.00},
    "4": {"id": "4", "name": "27\" Monitor", "price": 199.00},
    "5": {"id": "5", "name": "Noise-Canceling Headphones", "price": 149.00},
}

# carts: session_id -> { item_id: quantity }
carts = {}
lock = threading.Lock()

# ----------------------------
# Helpers
# ----------------------------
def get_or_create_session_id(from_request_only=False):
    """
    Retrieves session id from headers; if absent and from_request_only==False,
    create one and also set the response header later.
    """
    sid = request.headers.get("X-Session-Id")
    if sid:
        return sid
    if from_request_only:
        return None
    # create a new session id
    return str(uuid.uuid4())

def attach_session_header(resp, session_id):
    resp.headers["X-Session-Id"] = session_id
    return resp

def cart_totals(cart_items):
    subtotal = 0.0
    items_detailed = []
    for item_id, qty in cart_items.items():
        product = catalog.get(item_id)
        if not product:
            # Skip unknowns silently in totals
            continue
        line_total = round(product["price"] * qty, 2)
        subtotal = round(subtotal + line_total, 2)
        items_detailed.append({
            "id": product["id"],
            "name": product["name"],
            "unit_price": product["price"],
            "qty": qty,
            "line_total": line_total,
        })
    return items_detailed, subtotal

def get_cart(session_id):
    with lock:
        return carts.setdefault(session_id, {})

# ----------------------------
# Routes
# ----------------------------
@app.route("/healthz", methods=["GET"])
def health():
    log_event("healthz")
    resp = jsonify({"status": "ok"})
    return resp, 200

@app.route("/catalog", methods=["GET"])
def get_catalog():
    log_event("catalog.view")
    session_id = get_or_create_session_id()
    resp = jsonify({"catalog": list(catalog.values())})
    return attach_session_header(resp, session_id), 200

@app.route("/cart", methods=["GET"])
def view_cart():
    session_id = get_or_create_session_id()
    cart = get_cart(session_id)
    items, subtotal = cart_totals(cart)
    log_event("cart.view", subtotal=subtotal, items_count=len(items))
    resp = jsonify({"session_id": session_id, "items": items, "subtotal": subtotal})
    return attach_session_header(resp, session_id), 200

@app.route("/cart/add", methods=["POST"])
def add_to_cart():
    session_id = get_or_create_session_id()
    payload = request.get_json(silent=True) or {}
    item_id = str(payload.get("item_id", "")).strip()
    qty = int(payload.get("qty", 1))

    if not item_id or item_id not in catalog:
        log_event("cart.add.error", reason="invalid_item", item_id=item_id)
        resp = jsonify({"error": "Invalid or missing item_id"})
        return attach_session_header(resp, session_id), 400
    if qty <= 0:
        log_event("cart.add.error", reason="invalid_qty", qty=qty)
        resp = jsonify({"error": "qty must be positive"})
        return attach_session_header(resp, session_id), 400

    with lock:
        cart = carts.setdefault(session_id, {})
        cart[item_id] = cart.get(item_id, 0) + qty

    items, subtotal = cart_totals(cart)
    log_event("cart.add", item_id=item_id, qty=qty, subtotal=subtotal)
    resp = jsonify({"message": "added", "items": items, "subtotal": subtotal})
    return attach_session_header(resp, session_id), 200

@app.route("/cart/remove", methods=["POST"])
def remove_from_cart():
    session_id = get_or_create_session_id()
    payload = request.get_json(silent=True) or {}
    item_id = str(payload.get("item_id", "")).strip()
    qty = int(payload.get("qty", 1))

    if not item_id or item_id not in catalog:
        log_event("cart.remove.error", reason="invalid_item", item_id=item_id)
        resp = jsonify({"error": "Invalid or missing item_id"})
        return attach_session_header(resp, session_id), 400
    if qty <= 0:
        log_event("cart.remove.error", reason="invalid_qty", qty=qty)
        resp = jsonify({"error": "qty must be positive"})
        return attach_session_header(resp, session_id), 400

    with lock:
        cart = carts.setdefault(session_id, {})
        if item_id not in cart:
            log_event("cart.remove.error", reason="not_in_cart", item_id=item_id)
            resp = jsonify({"error": "Item not in cart"})
            return attach_session_header(resp, session_id), 400
        cart[item_id] -= qty
        if cart[item_id] <= 0:
            del cart[item_id]

    items, subtotal = cart_totals(cart)
    log_event("cart.remove", item_id=item_id, qty=qty, subtotal=subtotal)
    resp = jsonify({"message": "removed", "items": items, "subtotal": subtotal})
    return attach_session_header(resp, session_id), 200

@app.route("/cart", methods=["DELETE"])
def clear_cart():
    session_id = get_or_create_session_id()
    with lock:
        carts[session_id] = {}
    log_event("cart.clear")
    resp = jsonify({"message": "cleared"})
    return attach_session_header(resp, session_id), 200

@app.route("/checkout", methods=["POST"])
def checkout():
    session_id = get_or_create_session_id()
    with lock:
        cart = carts.get(session_id, {})
        items, subtotal = cart_totals(cart)
        if not items:
            log_event("checkout.error", reason="empty_cart")
            resp = jsonify({"error": "Cart is empty"})
            return attach_session_header(resp, session_id), 400
        order_id = str(uuid.uuid4())
        # Simulate payment success
        carts[session_id] = {}

    log_event("checkout.success", order_id=order_id, subtotal=subtotal, items=len(items))
    resp = jsonify({"order_id": order_id, "total": subtotal, "items": items})
    return attach_session_header(resp, session_id), 200

# Global error handler for unexpected exceptions
@app.errorhandler(Exception)
def handle_exception(e):
    log_event("server.error", error=str(e.__class__.__name__), message=str(e))
    resp = jsonify({"error": "internal_server_error", "message": str(e)})
    return resp, 500

if __name__ == "__main__":
    app.run(host=HOST, port=PORT, debug=DEBUG, threaded=True)
