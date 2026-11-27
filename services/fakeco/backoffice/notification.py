"""notification-service: turns notification messages into the rows the app shows."""
from .. import customers, db, queue
from ..app import create_app
from ._common import around, outside


async def handle(m, span):
    p = m["payload"]
    span.set_attributes({**await customers.attrs(p["customer_id"]), "notification.kind": m["kind"]})
    await outside("push.send", "push-gateway", around(.04), **{"notification.kind": m["kind"]})
    await db.execute("INSERT INTO notifications (customer_id, kind, text) VALUES (%s, %s, %s)", p["customer_id"], m["kind"], p["text"])
    return "completed"


def build():
    return create_app(workers=[lambda: queue.worker("notifications", handle)])
