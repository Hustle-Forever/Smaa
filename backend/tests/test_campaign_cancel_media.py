"""Cancelling a booked campaign item (or deleting its campaign) takes the
post's copies off the public bucket.

approved-instagram is anonymous-readable, so before this a cancelled campaign
booking left its media online for good (the Library's own cancel already
cleaned up). A post that was already published keeps its media: that is the
record of what went out. S3 runs against moto (`s3_storage` in conftest); the
stores are faked, so there is no database and no network.
"""
from __future__ import annotations

import asyncio

from app.config import config
from app.routers import campaigns as rt
from app.services import asset_storage
from app.services import campaign_store as cs
from app.services import scheduled_posts as sp


def _run(coro):
    return asyncio.run(coro)


async def _async(value):
    return value


def _host(s3_storage, *keys: str) -> list[str]:
    bucket = config.approved_media_bucket
    for k in keys:
        s3_storage.client.put_object(Bucket=bucket, Key=k, Body=b"j", ContentType="image/jpeg")
    return [asset_storage.public_url(bucket, k) for k in keys]


def _left(s3_storage) -> set[str]:
    listing = s3_storage.client.list_objects_v2(Bucket=config.approved_media_bucket)
    return {o["Key"] for o in listing.get("Contents", [])}


def _fake_posts(monkeypatch, posts: dict[int, dict]) -> list[int]:
    cancelled: list[int] = []

    async def _get(post_id):
        return posts.get(int(post_id))

    async def _cancel(post_id):
        cancelled.append(int(post_id))
        row = posts.get(int(post_id))
        return {**row, "status": "cancelled"} if row else None

    monkeypatch.setattr(sp, "get", _get)
    monkeypatch.setattr(sp, "cancel", _cancel)
    return cancelled


def _carousel_post(post_id: int, urls: list[str], status: str = "pending") -> dict:
    return {"id": post_id, "status": status, "media_url": urls[0], "metadata": {
        "is_carousel": True,
        "carousel_items": [{"media_url": u} for u in urls],
    }}


def test_cancel_item_deletes_every_hosted_slide(s3_storage, monkeypatch):
    urls = _host(s3_storage, "poster/1_ig_aa.jpg", "poster/2_ig_bb.jpg")
    _host(s3_storage, "poster/9_ig_keep.jpg")
    cancelled = _fake_posts(monkeypatch, {40: _carousel_post(40, urls)})
    item = {"id": 10, "status": cs.ITEM_SCHEDULED, "scheduled_post_id": 40}
    monkeypatch.setattr(rt.store, "get_item", lambda iid: _async(dict(item)))
    monkeypatch.setattr(rt.store, "cancel_item",
                        lambda iid: _async({**item, "status": cs.ITEM_CANCELLED}))

    out = _run(rt.cancel_item(10))

    assert out["status"] == cs.ITEM_CANCELLED
    assert cancelled == [40]
    assert _left(s3_storage) == {"poster/9_ig_keep.jpg"}


def test_a_published_post_keeps_its_media(s3_storage, monkeypatch):
    urls = _host(s3_storage, "poster/3_ig_cc.jpg")
    _fake_posts(monkeypatch, {41: {"id": 41, "status": "published", "media_url": urls[0],
                                   "metadata": {}}})
    item = {"id": 11, "status": cs.ITEM_SCHEDULED, "scheduled_post_id": 41}
    monkeypatch.setattr(rt.store, "get_item", lambda iid: _async(dict(item)))
    monkeypatch.setattr(rt.store, "cancel_item",
                        lambda iid: _async({**item, "status": cs.ITEM_CANCELLED}))

    _run(rt.cancel_item(11))

    assert _left(s3_storage) == {"poster/3_ig_cc.jpg"}


def test_cancel_item_survives_a_failing_booking_lookup(monkeypatch):
    async def _boom(post_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(sp, "get", _boom)
    item = {"id": 12, "status": cs.ITEM_SCHEDULED, "scheduled_post_id": 42}
    monkeypatch.setattr(rt.store, "get_item", lambda iid: _async(dict(item)))
    monkeypatch.setattr(rt.store, "cancel_item",
                        lambda iid: _async({**item, "status": cs.ITEM_CANCELLED}))

    assert _run(rt.cancel_item(12))["status"] == cs.ITEM_CANCELLED


def test_delete_campaign_unhosts_its_bookings(s3_storage, monkeypatch):
    a = _host(s3_storage, "poster/4_ig_dd.jpg")
    b = _host(s3_storage, "animation/5_ig_ee.mp4", "animation/6_ig_ff.mp4")
    kept = _host(s3_storage, "poster/7_ig_gg.jpg")
    cancelled = _fake_posts(monkeypatch, {
        50: {"id": 50, "status": "reminded", "media_url": a[0], "metadata": {}},
        51: _carousel_post(51, b),
        52: {"id": 52, "status": "published", "media_url": kept[0], "metadata": {}},
    })
    items = [
        {"id": 1, "status": cs.ITEM_SCHEDULED, "scheduled_post_id": 50},
        {"id": 2, "status": cs.ITEM_SCHEDULED, "scheduled_post_id": 51},
        # The item says published: its booking is not even cancelled.
        {"id": 3, "status": cs.ITEM_PUBLISHED, "scheduled_post_id": 52},
        {"id": 4, "status": cs.ITEM_APPROVED, "scheduled_post_id": None},
    ]
    monkeypatch.setattr(rt.store, "get_campaign", lambda cid: _async({"id": cid}))
    monkeypatch.setattr(rt.store, "items_for", lambda cid, limit=500: _async(items))
    monkeypatch.setattr(rt.store, "delete_campaign", lambda cid: _async(True))

    out = _run(rt.delete_campaign(1))

    assert out == {"deleted": True, "id": 1, "bookings_cancelled": 2}
    assert cancelled == [50, 51]
    assert _left(s3_storage) == {"poster/7_ig_gg.jpg"}
