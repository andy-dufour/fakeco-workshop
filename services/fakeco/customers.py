"""Customer names. The span attribute `app.customer.name` (the full name) goes wherever `app.customer.id` does."""
from . import db

_names = {}   # customer id -> full name; a customer's name does not change


async def attrs(customer_id):
    """`app.customer.id` and `app.customer.name` for a span."""
    if customer_id not in _names:
        _names.update({r["id"]: r["name"] for r in await db.fetch("SELECT id, name FROM customers", quiet=True)})
    out = {"app.customer.id": customer_id}
    if customer_id in _names:
        out["app.customer.name"] = _names[customer_id]
    return out
