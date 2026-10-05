"""Write tools: subscribers, tags, fields, events, purchases."""

import unittest

from support import BentoTestCase


class UpsertTests(BentoTestCase):
    def test_exact_body_tags_are_comma_strings_fields_flattened(self):
        out = self.call("bento_subscriber_upsert", subscribers=[{
            "email": "jane@example.com", "first_name": "Jane", "last_name": "Doe", "tags": ["vip", "newsletter"],
            "remove_tags": ["prospect"], "fields": {"plan": "pro", "seats": 3}, "utm_source": "ads",
            "created_at": "2025-01-02T03:04:05+00:00", "lifetime_value_currency": "usd", "lifetime_value_amount": 8000}])
        self.assertTrue(out["ok"], out)
        (req,) = self.server.hits("POST", "/batch/subscribers")
        self.assertEqual(req["body"], {"subscribers": [{
            "email": "jane@example.com", "first_name": "Jane", "last_name": "Doe", "utm_source": "ads",
            "tags": "vip,newsletter", "remove_tags": "prospect", "created_at": "2025-01-02T03:04:05Z",
            "lifetime_value_currency": "USD", "lifetime_value_amount": 8000, "plan": "pro", "seats": 3}]})
        self.assertTrue(any("5 minutes" in w for w in out["warnings"]))

    def test_dry_run_returns_payload_sends_nothing_and_flags_unknown_tags(self):
        out = self.call("bento_subscriber_upsert", subscribers=[{"email": "a@example.com", "tags": ["vip", "brand-new"]}], dry_run=True)
        self.assertEqual(out["request"]["body"]["subscribers"][0]["tags"], "vip,brand-new")
        self.assertEqual(out["context"]["tags_not_yet_defined"], ["brand-new"])
        self.assertEqual(self.server.posts(), [])
        self.assertEqual(out["tier"], "T1")

    def test_tier_flips_to_t2_above_bulk_threshold(self):
        subs = [{"email": f"a{i}@example.com"} for i in range(26)]
        self.assertEqual(self.call("bento_subscriber_upsert", subscribers=subs, dry_run=True)["tier"], "T2")
        self.ctx.config["bulk_threshold"] = 50
        self.assertEqual(self.call("bento_subscriber_upsert", subscribers=subs, dry_run=True)["tier"], "T1")

    def test_validation(self):
        bad = [[], "x", [{"first_name": "no email"}], [{"email": "bad"}], [{"email": "a@example.com", "tags": "vip"}],
               [{"email": "a@example.com", "tags": ["a,b"]}], [{"email": "a@example.com", "fields": {"email": "x"}}],
               [{"email": "a@example.com", "fields": {"k": {"nested": 1}}}], [{"email": "a@example.com", "bogus": 1}],
               [{"email": "a@example.com", "created_at": "yesterday"}], [{"email": "a@example.com", "created_at": "2025-01-01T00:00:00"}],
               [{"email": "a@example.com", "lifetime_value_currency": "USD"}],
               [{"email": "a@example.com", "lifetime_value_currency": "USD", "lifetime_value_amount": 10.5}],
               [{"email": "a@example.com"}] * 1001]
        for subs in bad:
            self.assertFalse(self.call("bento_subscriber_upsert", subscribers=subs)["ok"], str(subs)[:80])
        self.assertEqual(self.server.posts(), [])

    def test_duplicate_emails_in_batch_warn(self):
        out = self.call("bento_subscriber_upsert", subscribers=[{"email": "A@example.com"}, {"email": "a@example.com"}], dry_run=True)
        self.assertTrue(any("more than once" in w for w in out["warnings"]))


class TagCommandTests(BentoTestCase):
    def test_add_and_remove_command_bodies(self):
        self.assertTrue(self.call("bento_subscriber_tag", email="jane@example.com", action="add", tag="vip")["ok"])
        self.assertTrue(self.call("bento_subscriber_tag", email="jane@example.com", action="remove", tag="vip")["ok"])
        bodies = [r["body"] for r in self.server.hits("POST", "/fetch/commands")]
        self.assertEqual(bodies, [{"command": {"command": "add_tag", "email": "jane@example.com", "query": "vip"}},
                                  {"command": {"command": "remove_tag", "email": "jane@example.com", "query": "vip"}}])

    def test_validation(self):
        for args in ({"email": "bad", "action": "add", "tag": "x"}, {"email": "a@example.com", "action": "toggle", "tag": "x"},
                     {"email": "a@example.com", "action": "add", "tag": ""}, {"email": "a@example.com", "action": "add", "tag": "a,b"}):
            self.assertFalse(self.call("bento_subscriber_tag", **args)["ok"], args)
        self.assertEqual(self.server.posts(), [])


class ConsentTests(BentoTestCase):
    def test_subscribe_requires_reason(self):
        out = self.call("bento_subscriber_consent", email="jane@example.com", action="subscribe")
        self.assertIn("reason is required", out["error"])
        self.assertEqual(self.server.posts(), [])

    def test_subscribe_dry_run_shows_current_unsubscribe_state_and_warns(self):
        sub = {"data": {"attributes": {"unsubscribed_at": "2026-01-01T00:00:00Z"}}}
        self.server.route("GET", "/fetch/subscribers", {"json": sub})
        out = self.call("bento_subscriber_consent", email="jane@example.com", action="subscribe", reason="asked by email", dry_run=True)
        self.assertEqual(out["context"]["currently_unsubscribed_at"], "2026-01-01T00:00:00Z")
        self.assertTrue(any("RE-SUBSCRIBES" in w for w in out["warnings"]))
        self.assertEqual(out["request"]["body"], {"command": {"command": "subscribe", "email": "jane@example.com"}})
        self.assertEqual(self.server.posts(), [])

    def test_real_call_audit_carries_reason_not_in_request_body(self):
        out = self.call("bento_subscriber_consent", email="jane@example.com", action="subscribe", reason="asked by email")
        self.assertTrue(out["ok"])
        (req,) = self.server.hits("POST", "/fetch/commands")
        self.assertNotIn("reason", str(req["body"]))
        self.assertEqual(self.audit_lines()[0]["reason"], "asked by email")

    def test_unsubscribe_needs_no_reason(self):
        self.assertTrue(self.call("bento_subscriber_consent", email="jane@example.com", action="unsubscribe")["ok"])

    def test_bad_action(self):
        self.assertFalse(self.call("bento_subscriber_consent", email="jane@example.com", action="resubscribe")["ok"])


class DefinitionTests(BentoTestCase):
    def test_tag_create_body_and_cache_invalidation(self):
        self.call("bento_tag_list")
        out = self.call("bento_tag_create", name="spring-sale")
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.server.hits("POST", "/fetch/tags")[0]["body"], {"tag": {"name": "spring-sale"}})
        self.call("bento_tag_list")
        # GET 1 = first list; the create precheck reuses that fresh cache entry; the create invalidates it, so GET 2
        self.assertEqual(len(self.server.hits("GET", "/fetch/tags")), 2)

    def test_tag_create_lint(self):
        for name in ("Spring Sale", "UPPER", "-lead", "", "a" * 65, "x,y", "a b"):
            self.assertFalse(self.call("bento_tag_create", name=name)["ok"], name)
        self.assertEqual(self.server.posts(), [])

    def test_existing_tag_is_a_no_op(self):
        out = self.call("bento_tag_create", name="vip")
        self.assertTrue(out["ok"] and out["exists"] and not out["created"])
        self.assertEqual(self.server.posts(), [])

    def test_near_duplicate_refused(self):
        out = self.call("bento_tag_create", name="black_friday")
        self.assertTrue(out["refused"])
        self.assertIn("Black-Friday", out["error"])
        self.assertEqual(self.server.posts(), [])

    def test_field_create(self):
        self.assertTrue(self.call("bento_field_create", key="shoe_size")["ok"])
        self.assertEqual(self.server.hits("POST", "/fetch/fields")[0]["body"], {"field": {"key": "shoe_size"}})
        self.assertTrue(self.call("bento_field_create", key="plan")["exists"])
        self.assertFalse(self.call("bento_field_create", key="Shoe Size")["ok"])
        self.assertFalse(self.call("bento_field_create", key="1abc")["ok"])
        self.assertTrue(self.call("bento_field_create", key="firstname")["ok"] is False)  # near-dup of first_name
        self.assertEqual(len(self.server.hits("POST", "/fetch/fields")), 1)


class EventTests(BentoTestCase):
    def test_exact_body(self):
        out = self.call("bento_event_track", events=[{"type": "$signed_up", "email": "a@example.com", "fields": {"first_name": "Ari"},
                                                      "details": {"plan": {"tier": "pro"}}, "date": "2026-10-05T10:00:00-05:00"}])
        self.assertTrue(out["ok"], out)
        (req,) = self.server.hits("POST", "/batch/events")
        self.assertEqual(req["body"], {"events": [{"type": "$signed_up", "email": "a@example.com", "fields": {"first_name": "Ari"},
                                                   "details": {"plan": {"tier": "pro"}}, "date": "2026-10-05T15:00:00Z"}]})
        self.assertTrue(any("TRIGGER FLOWS" in w for w in out["warnings"]))

    def test_purchase_types_are_routed_to_the_purchase_tool(self):
        for t in ("$purchase", "purchase", "Order", "order_complete", "event_sale"):
            out = self.call("bento_event_track", events=[{"type": t, "email": "a@example.com"}])
            self.assertIn("bento_purchase_track", out["error"], t)
        self.assertEqual(self.server.posts(), [])

    def test_validation(self):
        bad = [[], [{"email": "a@example.com"}], [{"type": "x"}], [{"type": "x", "email": "a@example.com", "fields": {"a": [1]}}],
               [{"type": "x", "email": "a@example.com", "details": "str"}], [{"type": "x", "email": "a@example.com", "extra": 1}],
               [{"type": "x", "email": "a@example.com"}] * 1001]
        for events in bad:
            self.assertFalse(self.call("bento_event_track", events=events)["ok"], str(events)[:60])


class PurchaseTests(BentoTestCase):
    ARGS = {"email": "jane@example.com", "order_id": "order-123", "amount_cents": 8000, "currency": "usd"}

    def test_exact_body_matches_documented_unique_event_shape(self):
        out = self.call("bento_purchase_track", **self.ARGS, fields={"first_name": "Jane"},
                        items=[{"product_sku": "SKU1", "product_name": "Widget", "quantity": 2, "price_cents": 4000}],
                        date="2026-10-05T12:00:00Z")
        self.assertTrue(out["ok"], out)
        (req,) = self.server.hits("POST", "/batch/events")
        self.assertEqual(req["body"], {"events": [{
            "type": "$purchase", "email": "jane@example.com", "fields": {"first_name": "Jane"}, "date": "2026-10-05T12:00:00Z",
            "details": {"unique": {"key": "order-123"}, "value": {"currency": "USD", "amount": 8000},
                        "cart": {"items": [{"product_sku": "SKU1", "product_name": "Widget", "quantity": 2, "price": 4000}]}}}]})

    def test_amount_must_be_integer_cents(self):
        for bad in (80.0, "$80.00", "8000", True, -1, None):
            args = {**self.ARGS, "amount_cents": bad}
            out = self.call("bento_purchase_track", **args)
            self.assertFalse(out["ok"], bad)
            self.assertIn("amount_cents", out["error"])
        self.assertEqual(self.server.posts(), [])

    def test_order_id_is_mandatory_and_never_auto_generated(self):
        for bad in (None, "", "   "):
            args = {**self.ARGS, "order_id": bad}
            self.assertIn("order_id", self.call("bento_purchase_track", **args)["error"])
        args = dict(self.ARGS)
        del args["order_id"]
        self.assertFalse(self.call("bento_purchase_track", **args)["ok"])
        self.assertEqual(self.server.posts(), [])

    def test_currency_validation(self):
        for bad in ("US", "USDX", "12$", "", None, "us d"):
            self.assertFalse(self.call("bento_purchase_track", **{**self.ARGS, "currency": bad})["ok"], bad)

    def test_item_validation(self):
        bad_items = [[{"product_sku": "a", "quantity": 0}], [{"quantity": 1.5}], [{"price_cents": 9.99}], ["x"], [{"colour": "red"}], "nope"]
        for items in bad_items:
            self.assertFalse(self.call("bento_purchase_track", **self.ARGS, items=items)["ok"], items)

    def test_purchase_post_is_retried_because_unique_key_makes_it_idempotent(self):
        self.server.route("POST", "/batch/events", {"status": 503, "json": {}}, {"json": {"results": 1, "failed": 0}})
        out = self.call("bento_purchase_track", **self.ARGS)
        self.assertTrue(out["ok"], out)
        self.assertEqual(len(self.server.hits("POST", "/batch/events")), 2)

    def test_dry_run_preview_and_token_only_flow(self):
        out = self.call("bento_purchase_track", **self.ARGS, dry_run=True)
        self.assertEqual(out["summary"]["order_id"], "order-123")
        self.assertEqual(self.server.posts(), [])


if __name__ == "__main__":
    unittest.main()
