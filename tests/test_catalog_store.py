"""Regression coverage for persistent source catalog management."""
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from theaxiom import catalog_store, source_catalog


class CatalogStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "config" / "sources.json"
        self.store = catalog_store.CatalogStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_seeds_once_and_preserves_empty_catalog(self):
        initial = current = self.store.get()
        self.assertEqual(initial["revision"], 0)
        self.assertEqual([s["source_id"] for s in initial["sources"]],
                         [s["source_id"] for s in source_catalog.builtin_sources()])
        before = self.path.read_bytes()
        with mock.patch.object(source_catalog, "builtin_sources", side_effect=AssertionError("reseed")):
            self.assertEqual(catalog_store.CatalogStore(self.path).get(), initial)
            self.assertEqual(self.path.read_bytes(), before)
            for source in initial["sources"]:
                current = self.store.mutate("remove", source["source_id"], {}, current["revision"])
            self.assertEqual(json.loads(self.path.read_bytes())["sources"], [])
            self.assertEqual(catalog_store.CatalogStore(self.path).get(), current)

    def test_custom_source_crud_persists_across_store_restart(self):
        initial = self.store.get()
        saved = self.store.mutate("add", "my-reference", {
            "title": "My Reference", "description": "local candidate",
            "source_url": "https://example.org/reference.pdf", "publisher": "Example",
            "edition": "2026 edition", "language": "en", "expected_size_bytes": "12345",
            "sha256": "a" * 64, "provenance": "Verified from publisher catalog",
            "license": "CC BY 4.0",
        }, initial["revision"])
        self.assertEqual(saved["revision"], 1)
        source = next(x for x in saved["sources"] if x["source_id"] == "my-reference")
        self.assertEqual(source.keys(), {"source_id", "profile", "configuration"})
        self.assertEqual(source["configuration"]["expected_size_bytes"], "12345")
        self.assertEqual(source["configuration"]["sha256"], "a" * 64)
        self.assertTrue(self.path.exists())
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

        restarted = catalog_store.CatalogStore(self.path)
        loaded = restarted.get()
        self.assertIn("my-reference", [x["source_id"] for x in loaded["sources"]])
        edited = restarted.mutate("edit", "my-reference", {"title": "Updated"}, loaded["revision"])
        self.assertEqual(next(x for x in edited["sources"] if x["source_id"] == "my-reference")["profile"]["title"], "Updated")
        removed = restarted.mutate("remove", "my-reference", {}, edited["revision"])
        self.assertNotIn("my-reference", [x["source_id"] for x in removed["sources"]])

    def test_builtin_metadata_override_is_persistent_and_keeps_stable_id(self):
        initial = self.store.get()
        edited = self.store.mutate("edit", "wikipedia-en", {"title": "Reviewed title", "source_url": "https://example.org/candidate.zim", "sha256": "a" * 64, "license": "Candidate license"}, initial["revision"])
        current = next(s for s in edited["sources"] if s["source_id"] == "wikipedia-en")
        self.assertEqual(current["profile"]["title"], "Reviewed title")
        self.assertEqual(current["source_id"], "wikipedia-en")
        self.assertEqual(current["configuration"]["source_url"], "https://example.org/candidate.zim")
        self.assertEqual(current["configuration"]["sha256"], "a" * 64)
        self.assertEqual(current["configuration"]["license"], "Candidate license")
        restarted = catalog_store.CatalogStore(self.path).get()
        self.assertEqual(next(s for s in restarted["sources"] if s["source_id"] == "wikipedia-en")["profile"]["title"], "Reviewed title")

    def test_preloaded_remove_and_recreate_preserves_installed_content(self):
        content = Path(self.temp.name) / "content" / "wikipedia-en.zim"
        content.parent.mkdir()
        content.write_bytes(b"installed reference")
        current = self.store.get()
        original = next(s for s in current["sources"] if s["source_id"] == "wikipedia-en")
        record = next(s for s in json.loads(self.path.read_bytes())["sources"]
                      if s["source_id"] == "wikipedia-en")
        result = self.store.mutate("remove", "wikipedia-en", {}, current["revision"])
        self.assertNotIn("wikipedia-en", [x["source_id"] for x in result["sources"]])
        self.assertEqual(content.read_bytes(), b"installed reference")
        state = json.loads(self.path.read_text())
        self.assertNotIn("tombstones", state)
        self.assertEqual(state["schema_version"], catalog_store.SCHEMA_VERSION)
        restarted = catalog_store.CatalogStore(self.path)
        self.assertEqual(restarted.get(), result)
        fields = record["profile"] | record["configuration"]
        readded = restarted.mutate("add", "wikipedia-en", fields, result["revision"])
        self.assertEqual(next(s for s in readded["sources"] if s["source_id"] == "wikipedia-en"), original)
        self.assertEqual(content.read_bytes(), b"installed reference")

    def test_candidate_configuration_is_origin_independent(self):
        config = {"format": "zim", "source_url": "https://example.org/candidate.zim",
                  "sha256": "b" * 64, "expected_size_bytes": "1234", "license": "Candidate license"}
        edited = self.store.mutate("edit", "wikipedia-en", config, self.store.get()["revision"])
        added = self.store.mutate("add", "manual", {"title": "Manual", **config}, edited["revision"])
        records = {s["source_id"]: s for s in json.loads(self.path.read_bytes())["sources"]}
        for sid in ("wikipedia-en", "manual"):
            self.assertEqual({key: records[sid]["configuration"][key] for key in config}, config)
            self.assertFalse(config.keys() & records[sid]["profile"].keys())
            self.assertNotIn("seeded", records[sid])
        sources = {s["source_id"]: s for s in added["sources"]}
        wiki, manual = sources["wikipedia-en"], sources["manual"]
        self.assertEqual({key: wiki["configuration"][key] for key in config}, manual["configuration"])
        self.assertEqual(wiki.keys(), manual.keys())

    def test_revision_conflict_does_not_overwrite(self):
        revision = self.store.get()["revision"]
        self.store.mutate("add", "one", {"title": "One"}, revision)
        before = self.path.read_bytes()
        with self.assertRaises(catalog_store.RevisionConflict):
            self.store.mutate("add", "two", {"title": "Two"}, revision)
        self.assertEqual(self.path.read_bytes(), before)

    def test_busy_source_rejects_mutation(self):
        with self.assertRaisesRegex(catalog_store.CatalogError, "active or unresolved"):
            self.store.mutate("add", "busy", {"title": "Busy"}, 0, busy=True)
        self.assertFalse(self.path.exists())

    def test_concurrent_validation_and_revision_conflict(self):
        barrier = threading.Barrier(3, timeout=10)

        def add(sid, fields):
            store = catalog_store.CatalogStore(self.path)
            revision = store.get()["revision"]
            barrier.wait()
            try:
                store.mutate("add", sid, fields, revision)
            except catalog_store.RevisionConflict:
                return "conflict"
            except catalog_store.CatalogError:
                return "invalid"
            return "saved"

        with ThreadPoolExecutor(max_workers=3) as pool:
            calls = [pool.submit(add, sid, fields) for sid, fields in [
                ("one", {"title": "One"}), ("two", {"title": "Two"}),
                ("invalid", {"title": "Invalid", "sha256": "bad"}),
            ]]
            self.assertCountEqual([call.result() for call in calls], ["saved", "conflict", "invalid"])
        saved = self.store.get()
        self.assertEqual(saved["revision"], 1)
        ids = {s["source_id"] for s in saved["sources"]}
        self.assertEqual(len(ids & {"one", "two"}), 1)
        self.assertNotIn("invalid", ids)

    def test_v1_conversion_preserves_edits_deletions_and_revision(self):
        self.path.parent.mkdir()
        state = {"schema_version": 1, "revision": 7, "updated_at": "2026-10-01T12:00:00+00:00",
                 "custom": {"manual": {"title": "Manual", "sha256": "a" * 64}},
                 "overrides": {"wikipedia-en": {"title": "Edited", "sha256": "a" * 64},
                               "wikivoyage": {"title": "Deleted"}},
                 "tombstones": ["wikivoyage"]}
        self.path.write_text(json.dumps(state))
        before = self.path.read_bytes()
        with self.assertRaises(catalog_store.RevisionConflict):
            self.store.mutate("edit", "manual", {"title": "Stale"}, 6)
        self.assertEqual(self.path.read_bytes(), before)
        snapshot = self.store.get()
        self.assertEqual(snapshot["revision"], 7)
        self.assertEqual(snapshot["updated_at"], state["updated_at"])
        converted = self.path.read_bytes()
        self.assertEqual(set(json.loads(converted)), {"schema_version", "revision", "updated_at", "sources", "groups", "starter_groups_initialized", "seed_version"})
        sources = {s["source_id"]: s for s in snapshot["sources"]}
        self.assertNotIn("wikivoyage", sources)
        self.assertEqual(sources["wikipedia-en"]["profile"]["title"], "Edited")
        for sid in ("wikipedia-en", "manual"):
            self.assertEqual(sources[sid]["configuration"]["sha256"], "a" * 64)
        self.assertEqual(catalog_store.CatalogStore(self.path).get(), snapshot)
        self.assertEqual(self.path.read_bytes(), converted)

    def test_converts_unfinished_v2_without_trusting_saved_artifact_metadata(self):
        self.path.parent.mkdir()
        profile = next(s for s in source_catalog.builtin_sources() if s["source_id"] == "wikipedia-en")
        sid = profile.pop("source_id")
        record = {"source_id": sid, "profile": profile, "seeded": True, "configuration": {},
                  "candidate": {"sha256": "d" * 64}}
        state = {"schema_version": 2, "revision": 4, "updated_at": None, "sources": [record]}
        for invalid in [record | {"seeded": 1}, record | {"configuration": {"unexpected": True}},
                        record | {"profile": profile | {"bytes": 1}},
                        record | {"candidate": {"sha256": "invalid"}}]:
            with self.subTest(invalid=invalid):
                before = json.dumps(state | {"sources": [invalid]}).encode()
                self.path.write_bytes(before)
                with self.assertRaises(catalog_store.CatalogError):
                    self.store.get()
                self.assertEqual(self.path.read_bytes(), before)
        self.path.write_text(json.dumps(state))
        snapshot = self.store.get()
        self.assertEqual(snapshot["revision"], 4)
        source, = snapshot["sources"]
        self.assertEqual(source["configuration"]["sha256"], "d" * 64)
        converted, = json.loads(self.path.read_bytes())["sources"]
        self.assertEqual(converted["configuration"]["sha256"], "d" * 64)
        self.assertEqual(set(converted), {"source_id", "profile", "configuration"})

    def test_invalid_v1_is_not_replaced_even_if_bad_data_would_be_dropped(self):
        self.path.parent.mkdir()
        valid = {"schema_version": 1, "revision": 6, "updated_at": None,
                 "custom": {}, "overrides": {}, "tombstones": []}
        for changes in [
            {"schema_version": True}, {"schema_version": 1.0}, {"revision": -1},
            {"updated_at": []}, {"unexpected": True}, {"custom": []},
            {"custom": {"manual": {"description": "missing title"}}},
            {"custom": {"wikipedia-en": {"title": "Collision"}}},
            {"overrides": {"unknown": {"title": "Would be dropped"}}},
            {"overrides": {"wikivoyage": {"sha256": "bad"}}, "tombstones": ["wikivoyage"]},
            {"tombstones": ["unknown"]}, {"tombstones": [[]]},
            {"tombstones": ["wikivoyage", "wikivoyage"]},
        ]:
            with self.subTest(changes=changes):
                before = json.dumps(valid | changes).encode()
                self.path.write_bytes(before)
                with self.assertRaises(catalog_store.CatalogError):
                    self.store.get()
                self.assertEqual(self.path.read_bytes(), before)

    def test_rejects_invalid_ids_fields_urls_and_unknown_operations(self):
        for sid, fields, operation in [
            ("../bad", {"title": "Bad"}, "add"),
            ("good", {"title": "Bad", "unexpected": "x"}, "add"),
            ("good", {"title": "Bad", "source_url": "file:///etc/passwd"}, "add"),
            ("good", {"title": "Bad", "source_url": "https://user:pass@example.org/x"}, "add"),
            ("good", {"title": "Bad", "source_url": "https://example.org/../secret"}, "add"),
            ("good", {"source_url": "https://example.org/guide"}, "add"),
            ("good", {"title": "Bad", "expected_size_bytes": "0"}, "add"),
            ("good", {"title": "Bad", "expected_size_bytes": "1e9"}, "add"),
            ("good", {"title": "Bad", "sha256": "deadbeef"}, "add"),
            ("good", {"title": "Bad"}, "execute"),
        ]:
            with self.subTest(sid=sid, fields=fields, operation=operation):
                with self.assertRaises(catalog_store.CatalogError):
                    self.store.mutate(operation, sid, fields, 0)
        self.assertFalse(self.path.exists())

    def test_corrupt_catalog_fails_closed_without_replacement(self):
        self.path.parent.mkdir(parents=True)
        malformed_states = [
            "not-json",
            json.dumps({"schema_version": True, "revision": 0, "sources": []}),
            json.dumps({"schema_version": 2, "revision": 0, "sources": [{"source_id": "../bad"}]}),
            '{"schema_version":2,"revision":1,"revision":0,"updated_at":null,"sources":[]}',
        ]
        valid = {"source_id": "test", "profile": {"title": "Test"}, "configuration": {"format": "zim"}}
        malformed_states.extend(json.dumps({"schema_version": 2, "revision": 0, "updated_at": None,
                                            "sources": sources}) for sources in [
            [valid, valid], [valid | {"profile": {}}], [valid | {"profile": {"title": "\ud800"}}],
            [valid | {"configuration": {}}], [valid | {"configuration": {"format": []}}],
            [valid | {"configuration": {"format": "zim", "source_url": "file:///tmp/test.zim"}}],
            [valid | {"profile": {"title": "Test", "bytes": 123}}],
            [valid | {"artifact": {"sha256": "a" * 64}}],
        ])
        for malformed in malformed_states:
            with self.subTest(state=malformed):
                self.path.write_text(malformed)
                with self.assertRaises(catalog_store.CatalogError):
                    self.store.get()
                self.assertEqual(self.path.read_text(), malformed)
                with self.assertRaises(catalog_store.CatalogError):
                    self.store.mutate("add", "other", {"title": "Other"}, 0)
                self.assertEqual(self.path.read_text(), malformed)

    def test_group_memberships_and_restart(self):
        state = self.store.get()
        state = self.store.mutate("add", "manual", {"title": "Manual"}, state["revision"])
        original = state["sources"]
        for kind in ("categories", "collections", "tags"):
            state = self.store.mutate_group("create", kind, "reference", state["revision"], name="Reference")
            state = self.store.mutate_group("add-members", kind, "reference", state["revision"],
                                            source_ids=["manual", "wikipedia-en"])
        self.assertEqual(state["sources"], original)
        self.assertEqual(catalog_store.CatalogStore(self.path).get(), state)
        state = self.store.mutate_group("rename", "categories", "reference", state["revision"], name="Guides")
        state = self.store.mutate_group("remove-members", "tags", "reference", state["revision"], source_ids=["manual"])
        state = self.store.mutate_group("delete", "collections", "reference", state["revision"])
        self.assertEqual(state["sources"], original)
        self.assertEqual(next(g for g in state["groups"] if g["kind"] == "categories" and g["group_id"] == "reference")["name"], "Guides")
        self.assertEqual(next(g for g in state["groups"] if g["kind"] == "categories" and g["group_id"] == "reference")["source_ids"], ["manual", "wikipedia-en"])
        self.assertEqual(next(g for g in state["groups"] if g["kind"] == "tags" and g["group_id"] == "reference")["source_ids"], ["wikipedia-en"])
        state = self.store.mutate("remove", "manual", {}, state["revision"])
        self.assertTrue(all("manual" not in g["source_ids"] for g in state["groups"]))

    def test_default_groups_seed_fresh_and_never_automatically_reseed_deleted_group(self):
        initial = self.store.get()
        state = initial
        self.assertEqual(len(state["groups"]), len(catalog_store.DEFAULT_CATEGORIES) + len(catalog_store.DEFAULT_TAGS))
        self.assertTrue(all(not g["source_ids"] for g in state["groups"]))
        self.assertEqual(state["sources"], initial["sources"])
        state = self.store.mutate_group("delete", "categories", "communications", state["revision"])
        with self.assertRaises(catalog_store.CatalogError):
            self.store.mutate_group("initialize-categories", "categories", "defaults", state["revision"])
        self.assertEqual(self.store.get(), state)

    def test_merge_defaults_preserves_custom_groups_and_assignments(self):
        state = self.store.get()
        state = self.store.mutate_group("delete", "categories", "communications", state["revision"])
        state = self.store.mutate_group("rename", "tags", "winter", state["revision"], name="Cold season")
        state = self.store.mutate_group("add-members", "tags", "winter", state["revision"], source_ids=["wikipedia-en"])
        state = self.store.mutate_group("merge-defaults", "categories", "defaults", state["revision"])
        self.assertIn("communications", [g["group_id"] for g in state["groups"]])
        winter = next(g for g in state["groups"] if g["kind"] == "tags" and g["group_id"] == "winter")
        self.assertEqual(winter["name"], "Cold season")
        self.assertEqual(winter["source_ids"], ["wikipedia-en"])
        again = self.store.mutate_group("merge-defaults", "categories", "defaults", state["revision"])
        self.assertEqual(again["groups"], state["groups"])

    def test_existing_catalog_get_seeds_once_and_preserves_custom_memberships(self):
        original = self.store.get()
        saved = json.loads(self.path.read_bytes())
        saved.pop("starter_groups_initialized")
        saved["groups"] = [{"kind": "tags", "group_id": "winter", "name": "Cold season",
                            "source_ids": ["wikipedia-en"]}]
        self.path.write_text(json.dumps(saved))
        initialized = self.store.get()
        self.assertEqual(initialized["sources"], original["sources"])
        self.assertEqual(len(initialized["groups"]), len(catalog_store.DEFAULT_CATEGORIES) + len(catalog_store.DEFAULT_TAGS))
        winter = next(g for g in initialized["groups"] if g["group_id"] == "winter")
        self.assertEqual(winter["name"], "Cold season")
        self.assertEqual(winter["source_ids"], ["wikipedia-en"])
        self.assertTrue(json.loads(self.path.read_bytes())["starter_groups_initialized"])
        removed = self.store.mutate_group("delete", "categories", "communications", initialized["revision"])
        self.assertEqual(catalog_store.CatalogStore(self.path).get(), removed)

    def test_invalid_group_mutations_do_not_change_catalog(self):
        state = self.store.mutate_group("create", "tags", "one", self.store.get()["revision"], name="One")
        before = self.path.read_bytes()
        for operation, kind, gid, kwargs in [
            ("create", "tags", "two", {"name": "ONE"}),
            ("create", "bad", "two", {"name": "Two"}),
            ("add-members", "tags", "one", {"source_ids": ["unknown"]}),
            ("add-members", "tags", "one", {"source_ids": ["wikipedia-en", "wikipedia-en"]}),
            ("delete", "tags", "one", {"source_ids": []}),
        ]:
            with self.subTest(operation=operation, kwargs=kwargs):
                with self.assertRaises(catalog_store.CatalogError):
                    self.store.mutate_group(operation, kind, gid, state["revision"], **kwargs)
                self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaises(catalog_store.RevisionConflict):
            self.store.mutate_group("delete", "tags", "one", state["revision"] - 1)
        self.assertEqual(self.path.read_bytes(), before)

    def test_versioned_seed_catalog_is_only_imported_on_fresh_install(self):
        manifest = source_catalog.seed_catalog_manifest()
        self.assertEqual(manifest["version"], source_catalog.SEED_CATALOG_VERSION)
        self.assertIsInstance(manifest["version"], int)
        self.assertGreater(manifest["version"], 0)
        self.assertEqual(manifest["sources"], source_catalog.builtin_sources())
        manifest["sources"][0]["title"] = "Modified detached seed"
        self.assertNotEqual(manifest["sources"], source_catalog.builtin_sources())

        state = self.store.get()
        state = self.store.mutate("edit", "fema-ready", {"title": "Operator FEMA"}, state["revision"])
        state = self.store.mutate("remove", "wikipedia-en", {}, state["revision"])
        before = self.path.read_bytes()
        upgraded_seeds = [
            {"source_id": "fema-ready", "kind": "zim", "title": "New software default"},
            {"source_id": "new-default", "kind": "pdf", "title": "New suggested PDF"},
        ]
        with mock.patch.object(source_catalog, "builtin_sources", return_value=upgraded_seeds):
            self.assertEqual(self.store.get(), state)
            self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn("new-default", {item["source_id"] for item in state["sources"]})
        self.assertNotIn("wikipedia-en", {item["source_id"] for item in state["sources"]})

    def test_ready_gov_seed_and_existing_placeholder_never_auto_upgrade(self):
        expected = self.store._seed_record(source_catalog.READY_GOV_SOURCE)
        self.store._initialize_discovery(expected)
        initial = self.store.get()
        self.assertEqual(next(s for s in initial["sources"] if s["source_id"] == "fema-ready"), expected)
        self.assertEqual(expected["configuration"]["format"], "zim")
        self.assertTrue(expected["configuration"]["source_url"].endswith("ready.gov_en_all_2026-10.zim"))
        self.assertNotIn("sha256", expected["configuration"])
        self.assertNotIn("expected_size_bytes", expected["configuration"])
        old = self.store._record("fema-ready", {"title": "FEMA / Ready.gov",
                                              "description": "emergency preparedness library"})
        saved = json.loads(self.path.read_bytes())
        saved["sources"] = [old if row["source_id"] == "fema-ready" else row for row in saved["sources"]]
        saved["groups"][0]["source_ids"] = ["fema-ready"]
        self.path.write_text(json.dumps(saved))
        updated = self.store.get()
        self.assertEqual(updated["revision"], initial["revision"] + 1)
        old["profile"]["icon_png"] = expected["profile"]["icon_png"]
        self.assertEqual(next(s for s in updated["sources"] if s["source_id"] == "fema-ready"), old)
        self.assertEqual(updated["groups"][0]["source_ids"], ["fema-ready"])
        self.assertEqual(self.store.get(), updated)
        removed = self.store.mutate("remove", "fema-ready", {}, updated["revision"])
        self.assertEqual(self.store.get(), removed)

    def test_ready_gov_upgrade_does_not_overwrite_customized_placeholder(self):
        self.store.get()
        saved = json.loads(self.path.read_bytes())
        edited = self.store._record("fema-ready", {"title": "My emergency guide",
                                                 "description": "emergency preparedness library"})
        saved["sources"] = [edited if row["source_id"] == "fema-ready" else row for row in saved["sources"]]
        self.path.write_text(json.dumps(saved))
        result = self.store.get()
        edited["profile"]["icon_png"] = source_catalog.READY_GOV_SOURCE["icon_png"]
        self.assertEqual(next(s for s in result["sources"] if s["source_id"] == "fema-ready"), edited)

    def test_write_failure_preserves_prior_catalog(self):
        first = self.store.mutate("add", "one", {"title": "One"}, 0)
        before = self.path.read_bytes()
        with mock.patch.object(self.store, "_atomic_write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.mutate("edit", "one", {"title": "Changed"}, first["revision"])
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()

def test_periodic_opt_out_migrates_to_disabled_and_policy_controls_checks(tmp_path):
    from theaxiom import update_discovery_config as discovery
    store = catalog_store.CatalogStore(tmp_path / "sources.json")
    rev = store.get()["revision"]
    config = {"format": "zim", "source_url": "https://download.kiwix.org/zim/zimit/example_2026-10.zim",
              "update_method": "kiwix", "update_periodic": "no", "update_policy": "automatic"}
    saved = store.mutate("add", "fixture", {"title": "Fixture", **config}, rev)
    row = next(r for r in saved["sources"] if r["source_id"] == "fixture")
    assert "update_periodic" not in row["configuration"]
    assert row["configuration"]["update_policy"] == "disabled"
    for policy in ("notify", "automatic", "scheduled"):
        updated = row["configuration"] | {"update_policy": policy}
        if policy == "scheduled":
            updated |= {"update_window_start": "01:00", "update_window_end": "02:00"}
        assert discovery.settings(updated)["periodic"]
    assert not discovery.settings(row["configuration"])["periodic"]


def test_hearham_seed_upgrade_preserves_existing_catalog(tmp_path):
    store = catalog_store.CatalogStore(tmp_path / "sources.json")
    store.get()
    saved = json.loads(store.path.read_bytes())
    saved["seed_version"] = 4
    saved["sources"] = [r for r in saved["sources"] if r["source_id"] not in {"hearham", "mdwiki"}]
    saved["sources"][0]["profile"]["title"] = "Operator title"
    store.path.write_text(json.dumps(saved))
    upgraded = store.get()
    assert upgraded["revision"] == saved["revision"] + 1
    assert upgraded["sources"][:-2] == saved["sources"]
    assert upgraded["groups"] == saved["groups"]
    assert [row["source_id"] for row in upgraded["sources"][-2:]] == ["hearham", "mdwiki"]
    removed = store.mutate("remove", "hearham", {}, upgraded["revision"])
    assert store.get() == removed
    assert json.loads(store.path.read_bytes())["seed_version"] == 7


def test_mdwiki_seed_upgrade_is_opt_in_and_preserves_operator_changes(tmp_path):
    from theaxiom import update_discovery_config, zim_source

    store = catalog_store.CatalogStore(tmp_path / "sources.json")
    fresh = store.get()
    row = next(r for r in fresh["sources"] if r["source_id"] == "mdwiki")
    config = row["configuration"]
    assert config["format"] == "zim"
    assert zim_source.candidate(config) == (
        "https://lb.download.kiwix.org/zim/other/mdwiki_en_all_maxi_2025-11.zim",
        2302838768, "ee51df7860dbcab8c628aad32cf9972726ab708d784cdaf3e217d596920edf80",
    )
    assert config["update_policy"] == "notify"
    assert update_discovery_config.settings(config)["method"] == "kiwix"
    assert row["profile"]["icon_png"]
    assert "professional medical advice" in row["profile"]["description"]
    assert "CC BY-SA" in config["license"]
    saved = json.loads(store.path.read_bytes())
    saved["seed_version"] = 5
    saved["sources"] = [r for r in saved["sources"] if r["source_id"] != "mdwiki"]
    saved["sources"][0]["profile"]["title"] = "Operator custom title"
    store.path.write_text(json.dumps(saved))
    upgraded = store.get()
    assert upgraded["revision"] == saved["revision"] + 1
    assert upgraded["sources"][:-1] == saved["sources"]
    assert upgraded["sources"][-1] == row
    assert upgraded["groups"] == saved["groups"]
    edited = store.mutate("edit", "mdwiki", {"title": "My medical reference"}, upgraded["revision"])
    assert store.get() == edited
    removed = store.mutate("remove", "mdwiki", {}, edited["revision"])
    assert store.get() == removed
    assert json.loads(store.path.read_bytes())["seed_version"] == 7


# Exact placeholder shipped in seed catalog v6 (not a replacement logo).
_LEGACY_MDWIKI_ICON = "iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAJI0lEQVR4AQEYCef2AAAAAAAAAAAAAAAAAAAAAAAZam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAGWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAZam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/wAAAAAAAAAAAAAAAAAAAAAAGWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8AAAAAAAAAAAAZam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8AGWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt//////////////////////8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/ABlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf//////////////////////GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/wAZam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3//////////////////////xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8AGWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt//////////////////////8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/ABlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf//////////////////////GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/wAZam3/GWpt/xlqbf8Zam3/GWpt////////////////////////////////////////////////////////////////////////////GWpt/xlqbf8Zam3/GWpt/xlqbf8AGWpt/xlqbf8Zam3/GWpt/xlqbf///////////////////////////////////////////////////////////////////////////xlqbf8Zam3/GWpt/xlqbf8Zam3/ABlqbf8Zam3/GWpt/xlqbf8Zam3///////////////////////////////////////////////////////////////////////////8Zam3/GWpt/xlqbf8Zam3/GWpt/wAZam3/GWpt/xlqbf8Zam3/GWpt////////////////////////////////////////////////////////////////////////////GWpt/xlqbf8Zam3/GWpt/xlqbf8AGWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt//////////////////////8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/ABlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf//////////////////////GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/wAZam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3//////////////////////xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8AGWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt//////////////////////8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/ABlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf//////////////////////GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/wAZam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8AAAAAAAAAAAAZam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/wAAAAAAAAAAAAAAAAAAAAAAGWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAGWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8Zam3/GWpt/xlqbf8AAAAAAAAAAAAAAAAAAAAAQoDCDTwkb74AAAAASUVORK5CYII="


def test_versioned_icon_replacement_preserves_operator_settings(tmp_path):
    from hashlib import sha256

    seed = source_catalog.source_defaults()["mdwiki"]
    assert seed["icon_update"]["previous_sha256"] == sha256(_LEGACY_MDWIKI_ICON.encode("ascii")).hexdigest()
    assert seed["icon_png"] != _LEGACY_MDWIKI_ICON
    store = catalog_store.CatalogStore(tmp_path / "sources.json")
    store.get()
    saved = json.loads(store.path.read_bytes())
    saved["seed_version"] = 6
    row = next(r for r in saved["sources"] if r["source_id"] == "mdwiki")
    row["profile"]["title"] = "My medical reference"
    row["profile"]["icon_png"] = _LEGACY_MDWIKI_ICON
    row["configuration"]["update_policy"] = "disabled"
    store.path.write_text(json.dumps(saved))
    upgraded = store.get()
    row["profile"]["icon_png"] = seed["icon_png"]
    assert upgraded["sources"] == saved["sources"]
    assert upgraded["groups"] == saved["groups"]
    assert upgraded["revision"] == saved["revision"] + 1
    assert store.get() == upgraded
    assert json.loads(store.path.read_bytes())["seed_version"] == 7


def test_versioned_icon_replacement_preserves_custom_blank_and_deleted_sources(tmp_path):
    for index, choice in enumerate((source_catalog.READY_GOV_SOURCE["icon_png"], "", None)):
        store = catalog_store.CatalogStore(tmp_path / str(index) / "sources.json")
        store.get()
        saved = json.loads(store.path.read_bytes())
        saved["seed_version"] = 6
        if choice is None:
            saved["sources"] = [r for r in saved["sources"] if r["source_id"] != "mdwiki"]
        else:
            row = next(r for r in saved["sources"] if r["source_id"] == "mdwiki")
            row["profile"]["icon_png"] = choice
        store.path.write_text(json.dumps(saved))
        upgraded = store.get()
        assert upgraded["sources"] == saved["sources"]
        assert store.get() == upgraded


def test_internet_archive_starter_is_bounded_opt_in_and_preserves_custom_configuration(tmp_path):
    from theaxiom import pdf_source, update_discovery_config

    store = catalog_store.CatalogStore(tmp_path / "sources.json")
    state = store.get()
    sid = "internet-archive"
    row = next(r for r in state["sources"] if r["source_id"] == sid)
    setup = source_catalog.source_defaults()[sid]["setup"]
    pin = pdf_source.pin(sid, row["configuration"])
    assert pin["bytes"] == 3904570
    assert pin["sha256"] == "b3416ab75ff1986947bc28e52045ea2eb81fcc5a3d33c4723c6cafc20fc3d942"
    assert row["configuration"]["publisher_sha1"] == "e7c2380a5222d5c1677924a1e5f804e9ced69f83"
    assert row["configuration"]["license"].startswith("Public domain")
    assert row["profile"]["author"] == setup["author"]
    assert row["profile"]["icon_png"]
    assert update_discovery_config.settings(row["configuration"]) == dict(
        method="head", endpoint=pin["source_url"], periodic=True)
    # An old unconfigured placeholder gets the defaults on an existing install.
    saved = json.loads(store.path.read_bytes())
    old = next(r for r in saved["sources"] if r["source_id"] == sid)
    old["configuration"] = {"format": "unspecified"}
    old["profile"]["title"] = "Internet Archive"
    old["profile"]["description"] = "curated offline reference collections"
    old["profile"].pop("author", None)
    store.path.write_text(json.dumps(saved))
    upgraded = store.get()
    current = next(r for r in upgraded["sources"] if r["source_id"] == sid)
    assert current["configuration"] == row["configuration"]
    assert current["profile"]["title"] == setup["title"]
    assert upgraded["groups"] == saved["groups"]
    custom = store.mutate("edit", sid, {"title": "My archive",
        "source_url": "https://example.org/other.pdf"}, upgraded["revision"])
    assert store.get() == custom
    removed = store.mutate("remove", sid, {}, custom["revision"])
    assert store.get() == removed
